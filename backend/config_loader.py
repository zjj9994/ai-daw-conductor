"""配置加载：合并 config.yaml 与环境变量。

优先级（从高到低）：
  1. 环境变量（AI_API_KEY / AI_MODEL / AI_BASE_URL ...）
  2. config/config.yaml
  3. config/config.example.yaml（仅作为字段参考，不含密钥）
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data


def load_config() -> dict[str, Any]:
    """加载并合并配置。"""
    # example 仅用于补全缺失字段，不依赖其密钥
    base = _load_yaml(CONFIG_DIR / "config.example.yaml")
    user = _load_yaml(CONFIG_DIR / "config.yaml")
    cfg = _deep_merge(base, user)

    # 环境变量覆盖
    ai = cfg.setdefault("ai", {})
    if os.getenv("AI_API_KEY"):
        ai["api_key"] = os.getenv("AI_API_KEY")
    if os.getenv("AI_BASE_URL"):
        ai["base_url"] = os.getenv("AI_BASE_URL")
    if os.getenv("AI_MODEL"):
        ai["model"] = os.getenv("AI_MODEL")
    if os.getenv("AI_PROVIDER"):
        ai["provider"] = os.getenv("AI_PROVIDER")

    server = cfg.setdefault("server", {})
    if os.getenv("SERVER_PORT"):
        server["port"] = int(os.getenv("SERVER_PORT"))

    return cfg


def is_macos() -> bool:
    return os.uname().sysname == "Darwin"
