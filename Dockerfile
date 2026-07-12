# ====================================================================
# AI-DAW-Conductor · Docker 镜像
# 用于非 macOS 环境下的开发 / 测试 / demo 体验
#
# 说明：
#   - 容器内可运行 FastAPI 服务、Playwright（Chromium）网页 AI 驱动、MIDI 生成
#   - Logic Pro 实控（AppleScript）在容器内不可用，自动降级为模拟模式
#   - python-rtmidi 实时 MIDI 输出在容器内不可用，MIDI 仍可写入文件
#   - 推荐用 CDP 模式连接宿主机上已登录网页 AI 的 Chrome
#     （启动 Chrome 时加 --remote-debugging-port=9222 --remote-debugging-address=0.0.0.0）
#
# 构建：docker build -t ai-daw-conductor .
# 运行：docker run -p 8787:8787 -e BROWSER_CDP_URL=http://host.docker.internal:9222 ai-daw-conductor
# ====================================================================
FROM python:3.11-slim

# 避免交互式安装
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive

# 系统依赖：Playwright Chromium 运行所需的库 + 中文字体（网页 AI 多为中文界面）
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates \
        fonts-noto-cjk \
        libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 \
        libcups2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
        libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 \
        libatspi2.0-0 libdrm2 libxshmfence1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先装依赖（利用 Docker 层缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install chromium

# 复制项目
COPY backend/ ./backend/
COPY frontend/ ./frontend/
COPY config/ ./config/

# 渲染输出目录（模拟模式下导出占位文件）
RUN mkdir -p /root/Music/AI-DAW-Conductor/renders

EXPOSE 8787

# 健康检查：FastAPI /api/health
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8787/api/health || exit 1

CMD ["python", "-m", "uvicorn", "backend.server:app", "--host", "0.0.0.0", "--port", "8787"]
