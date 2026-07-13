"""单元测试：网页 AI provider 目录与切换逻辑。

覆盖 ai_engine.PROVIDER_CATALOG / list_providers / get_provider_meta，
以及 config_loader.VALID_PROVIDERS 与目录的一致性。
运行 `pytest tests/test_provider_catalog.py`
"""
from backend.ai_engine import (
    PROVIDER_CATALOG, DEFAULT_URLS, list_providers, get_provider_meta,
)
from backend.config_loader import VALID_PROVIDERS


def test_catalog_has_required_keys():
    """每个 provider 条目必须含 name/url/initial/color/vendor/region/order。"""
    required = {"name", "url", "initial", "color", "vendor", "region", "order"}
    for key, meta in PROVIDER_CATALOG.items():
        missing = required - set(meta.keys())
        assert not missing, f"provider {key} 缺字段：{missing}"


def test_catalog_includes_mainstream_ais():
    """至少包含 13+ 个主流网页 AI + custom 兜底。"""
    expected = {
        "doubao", "kimi", "qwen", "zhipu", "deepseek",
        "chatgpt", "claude", "gemini", "grok", "perplexity", "custom",
    }
    assert expected.issubset(set(PROVIDER_CATALOG.keys()))
    assert len(PROVIDER_CATALOG) >= 13


def test_custom_has_empty_url():
    """custom 兜底项网址应为空（需用户填写）。"""
    assert PROVIDER_CATALOG["custom"]["url"] == ""


def test_non_custom_providers_have_url():
    """除 custom 外，每个 provider 必须有非空默认网址。"""
    for key, meta in PROVIDER_CATALOG.items():
        if key == "custom":
            continue
        assert meta["url"], f"provider {key} 缺默认网址"
        assert meta["url"].startswith("https://"), f"{key} 网址应为 https"


def test_default_urls_derived_from_catalog():
    """DEFAULT_URLS 应由 PROVIDER_CATALOG 派生，保持单一真相源。"""
    assert set(DEFAULT_URLS.keys()) == set(PROVIDER_CATALOG.keys())
    for k, v in PROVIDER_CATALOG.items():
        assert DEFAULT_URLS[k] == v["url"]


def test_list_providers_sorted_by_order():
    """list_providers 返回按 order 升序排列，且含 key 字段。"""
    items = list_providers()
    assert len(items) == len(PROVIDER_CATALOG)
    orders = [it["order"] for it in items]
    assert orders == sorted(orders)
    # custom 排最后
    assert items[-1]["key"] == "custom"
    # 每条含 key 字段供前端使用
    for it in items:
        assert "key" in it and "name" in it and "url" in it


def test_get_provider_meta_known():
    meta = get_provider_meta("doubao")
    assert meta["name"] == "豆包"
    assert meta["url"].startswith("https://")


def test_get_provider_meta_unknown_falls_back_to_custom():
    """未知 provider 回退到 custom，不抛异常。"""
    meta = get_provider_meta("nonexistent-ai")
    assert meta["name"] == "自定义"


def test_valid_providers_matches_catalog_keys():
    """config_loader.VALID_PROVIDERS 应与 PROVIDER_CATALOG 的键完全一致。"""
    assert set(VALID_PROVIDERS) == set(PROVIDER_CATALOG.keys())


def test_new_providers_accepted_by_config_validator():
    """新增的 provider 应能通过配置校验（不被标记为非法）。"""
    from backend.config_loader import validate_config
    for key in ("deepseek", "chatgpt", "claude", "gemini", "grok", "perplexity"):
        cfg = {
            "ai": {"provider": key},
            "browser": {"mode": "cdp", "cdp_url": "http://127.0.0.1:9222"},
            "server": {"port": 8787},
        }
        issues = validate_config(cfg)
        assert not any("provider" in i for i in issues), f"{key} 应合法：{issues}"
