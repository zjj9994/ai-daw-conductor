#!/usr/bin/env bash
# 启动一个带远程调试端口的 Chrome，用于让后端通过 CDP 复用你的登录会话。
#
# 用法：
#   ./scripts/launch_chrome.sh            # 默认 9222 端口
#   PORT=9333 ./scripts/launch_chrome.sh  # 自定义端口
#
# 启动后在弹出的 Chrome 里登录你的网页 AI（豆包/Kimi/通义千问/智谱清言），
# 然后回到网页前端点「设置 → 测试连接」即可。后端不会读取你的账号密码。
set -e

PORT="${PORT:-9222}"
PROFILE="${CHROME_PROFILE:-$HOME/.ai-daw-conductor/chrome-profile}"
mkdir -p "$PROFILE"

# 找 Chrome / Chromium / Edge 可执行文件
find_browser() {
  for cand in \
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge" \
    "/Applications/Chromium.app/Contents/MacOS/Chromium" \
    "/usr/bin/google-chrome" "/usr/bin/google-chrome-stable" \
    "/usr/bin/chromium" "/usr/bin/chromium-browser" \
    "/usr/bin/microsoft-edge" "/usr/bin/microsoft-edge-stable"; do
    [ -x "$cand" ] && { echo "$cand"; return; }
  done
  echo ""
}

BROWSER="$(find_browser)"
if [ -z "$BROWSER" ]; then
  echo "未找到 Chrome / Chromium / Edge。请安装后重试，或在 config 里改用 persistent 模式。"
  exit 1
fi

echo "▶ 启动浏览器：$BROWSER"
echo "  · 调试端口：$PORT  （config.browser.cdp_url 设为 http://127.0.0.1:$PORT）"
echo "  · 用户目录：$PROFILE"
echo "  · 启动后请在该浏览器里登录你的网页 AI，再回到前端点「测试连接」。"
echo ""

exec "$BROWSER" \
  --remote-debugging-port="$PORT" \
  --user-data-dir="$PROFILE" \
  --no-first-run \
  --no-default-browser-check \
  "https://www.doubao.com/chat/"