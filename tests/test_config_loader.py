"""单元测试：配置校验。运行 `pytest tests/`"""
from backend.config_loader import validate_config, VALID_PROVIDERS, VALID_BROWSER_MODES


def test_valid_config_returns_no_issues():
    cfg = {
        "ai": {"provider": "doubao", "timeout": 180},
        "browser": {"mode": "cdp", "cdp_url": "http://127.0.0.1:9222"},
        "server": {"port": 8787},
    }
    assert validate_config(cfg) == []


def test_invalid_provider_flagged():
    cfg = {"ai": {"provider": "openai"}, "browser": {"mode": "cdp"}, "server": {"port": 8787}}
    issues = validate_config(cfg)
    assert any("provider" in i for i in issues)


def test_invalid_browser_mode_flagged():
    cfg = {"ai": {"provider": "kimi"}, "browser": {"mode": "selenium"}, "server": {"port": 8787}}
    issues = validate_config(cfg)
    assert any("browser.mode" in i for i in issues)


def test_timeout_too_small_flagged():
    cfg = {"ai": {"provider": "qwen", "timeout": 5}, "browser": {"mode": "cdp"}, "server": {"port": 8787}}
    issues = validate_config(cfg)
    assert any("timeout" in i for i in issues)


def test_invalid_port_flagged():
    cfg = {"ai": {"provider": "zhipu"}, "browser": {"mode": "cdp"}, "server": {"port": 99999}}
    issues = validate_config(cfg)
    assert any("port" in i for i in issues)


def test_cdp_url_must_start_with_http():
    cfg = {"ai": {"provider": "doubao"}, "browser": {"mode": "cdp", "cdp_url": "127.0.0.1:9222"}, "server": {"port": 8787}}
    issues = validate_config(cfg)
    assert any("cdp_url" in i for i in issues)


def test_persistent_mode_skips_cdp_url_check():
    cfg = {"ai": {"provider": "doubao"}, "browser": {"mode": "persistent"}, "server": {"port": 8787}}
    assert validate_config(cfg) == []


def test_valid_providers_set_contents():
    assert "doubao" in VALID_PROVIDERS
    assert "kimi" in VALID_PROVIDERS
    assert "qwen" in VALID_PROVIDERS
    assert "zhipu" in VALID_PROVIDERS
    assert "custom" in VALID_PROVIDERS


def test_valid_browser_modes_set_contents():
    assert "cdp" in VALID_BROWSER_MODES
    assert "persistent" in VALID_BROWSER_MODES


def test_missing_sections_uses_defaults():
    """空配置应能跑通校验（用默认值），不应抛异常。"""
    issues = validate_config({})
    # 默认 provider=doubao, mode=cdp, timeout=180, port=8787，但 cdp_url 默认空会被标记
    assert any("cdp_url" in i for i in issues)
