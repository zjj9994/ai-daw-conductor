#!/usr/bin/env bash
# 一键安装脚本（macOS / Linux 通用）
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

# 2. 系统依赖提示
if [ "$(uname)" = "Darwin" ]; then
  if ! command -v brew >/dev/null 2>&1; then
    echo "  · 检测到 macOS，建议安装 Homebrew 以便编译 python-rtmidi"
  fi
else
  echo "  · 非 macOS：AppleScript 将以模拟模式运行（仅生成 MIDI 文件）"
fi

echo "  · 安装 Python 依赖"
pip install --upgrade pip >/dev/null
pip install -r requirements.txt

# 3. Playwright 浏览器（仅 persistent 模式需要；CDP 模式连接你本机 Chrome 无需下载）
echo "  · 检查 Playwright 浏览器（persistent 模式才需要，可跳过）"
python3 -m playwright install chromium 2>/dev/null || echo "  · 跳过 Playwright 浏览器下载（CDP 模式不需要）"

# 4. 配置文件
if [ ! -f "config/config.yaml" ]; then
  echo "  · 生成 config/config.yaml（无需 API Key，默认复用网页版豆包）"
  cp config/config.example.yaml config/config.yaml
fi

# 5. 渲染目录
mkdir -p "$HOME/Music/AI-DAW-Conductor/renders"

echo ""
echo "✓ 安装完成"
echo "  · 启动调试 Chrome（并在其中登录网页 AI）：  ./scripts/launch_chrome.sh"
echo "  · 启动服务：  ./scripts/run.sh"
echo "  · 打开页面：  http://127.0.0.1:8787"
echo "  · 在网页「设置」里点「测试连接」确认已连接网页 AI。"
