#!/usr/bin/env bash
# 一键安装与启动脚本（macOS / Linux 通用）
set -e

cd "$(dirname "$0")/.."
ROOT="$(pwd)"

echo "▶ AI-DAW-Conductor 安装"

# 1. Python 虚拟环境
if [ ! -d ".venv" ]; then
  echo "  · 创建虚拟环境 .venv"
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# 2. 依赖（macOS 需先装 rtmidi 系统依赖）
if [ "$(uname)" = "Darwin" ]; then
  if ! command -v brew >/dev/null 2>&1; then
    echo "  · 检测到 macOS，建议安装 Homebrew 以便编译 python-rtmidi"
  fi
else
  echo "  · 非 macOS：MIDI/AppleScript 将以模拟模式运行（仅生成 MIDI 文件）"
fi

echo "  · 安装 Python 依赖"
pip install --upgrade pip >/dev/null
pip install -r requirements.txt

# 3. 配置文件
if [ ! -f "config/config.yaml" ]; then
  echo "  · 生成 config/config.yaml（请编辑后填入 Ark API Key）"
  cp config/config.example.yaml config/config.yaml
fi

# 4. 渲染目录
mkdir -p "$HOME/Music/AI-DAW-Conductor/renders"

echo ""
echo "✓ 安装完成"
echo "  · 编辑配置：  $ROOT/config/config.yaml"
echo "  · 启动服务：  ./scripts/run.sh"
echo "  · 打开页面：  http://127.0.0.1:8787"
