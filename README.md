# 🎹 AI-DAW-Conductor

> 复用**你已登录的网页端 AI**（网页版豆包 / Kimi / 通义千问 / 智谱清言）指挥 Logic Pro，让 AI **独立完成** 作曲 → 编曲 → 混音 → 母带 全流程。**不使用 API、不需要 API Key。**

在控制台网页里用自然语言描述你想要的歌曲，后端用 Playwright 接管你浏览器里那个已经登录的网页 AI 窗口，把「系统指令 + 阶段要求 + 你的描述」注入对话框，抓取 AI 回复的结构化 JSON，再翻译成 MIDI、轨道、混音参数与插件操作，自动驱动 Logic Pro 完成制作并导出母带。

```
┌────────────┐   WebSocket    ┌──────────────────┐  MIDI+AppleScript  ┌───────────┐
│  控制台网页  │ ◄────────────► │   本地后端服务     │ ─────────────────► │ Logic Pro  │
│ (浏览器)    │                │  FastAPI + WS    │                    │  (macOS)   │
└────────────┘                └────────┬─────────┘                    └───────────┘
                                       │ Playwright (CDP)
                                       ▼
                             ┌──────────────────────┐
                             │ 你已登录的网页 AI 标签页 │  ← 豆包 / Kimi / 千问 …
                             │ (你的真实账号会话)       │
                             └──────────────────────┘
```

## ✨ 能做什么

AI 在四个阶段独立完成决策与执行：

| 阶段 | AI 决策 | 对 Logic Pro 的操作 |
|------|---------|--------------------|
| **作曲 Compose** | 标题、风格、速度、调性、段落结构、主旋律与和弦 | 新建项目、建软件乐器轨、生成 MIDI 并导入 |
| **编曲 Arrange** | 鼓组/贝斯/和声铺底/副旋律配置与各声部音符 | 新增轨道、按段落写入 MIDI 片段 |
| **混音 Mix** | 音量、声相、EQ、压缩、发送、效果链 | 设置混音器参数、挂插件、建 Reverb Bus |
| **母带 Master** | 母带链（EQ→压缩→限制器）与导出参数 | 主输出挂母带插件、Bounce 导出 wav/aiff/mp3 |

## 🔑 为什么不用 API？

按需求，本项目**不调用任何大模型 API**，而是直接复用你在浏览器里登录好的网页 AI 会话：

- 你像往常一样打开网页版豆包 / Kimi / 千问并登录；
- 后端通过 Chrome DevTools Protocol（CDP）连接到这个浏览器进程，在 AI 窗口里代你输入提示词、读取流式回复；
- 全程不接触账号密码、不申请 API Key、不产生 API 计费；
- 你的会员权益、长上下文、联网搜索等网页 AI 原有能力全部保留。

## 🧱 架构

```
ai-daw-conductor/
├── backend/
│   ├── server.py            # FastAPI + WebSocket，前端入口与事件流
│   ├── ai_engine.py         # WebAIDriver(Playwright) 驱动网页 AI + 内置 demo 生成器 + 自评估
│   ├── autonomous.py        # 自主流水线：不间断完成整首作品（自评估+重连+上下文累积）
│   ├── commander.py         # 把 AI 决策编排为有序执行（流水线）
│   ├── daw_controller.py    # 高层动作：建项目/轨道/MIDI/混音/母带/导出
│   ├── midi_engine.py       # 生成标准 MIDI 文件 + 虚拟端口实时输出（mido/rtmidi）
│   ├── applescript_bridge.py# osascript 控制 Logic Pro（轨道/混音器/插件/Bounce）
│   ├── music_theory.py      # 音名互转、音阶、和弦、量化等乐理工具
│   ├── models.py            # pydantic 数据模型（StageResult / TrackSpec / MixParams ...）
│   ├── task_tracker.py      # 任务状态/进度/渲染历史追踪（供轮询）
│   ├── diagnostics.py       # 各子系统可用性诊断 + 建议
│   ├── config_loader.py     # 合并 config.yaml 与环境变量 + 配置校验
│   └── logging_config.py    # 文件日志（按天轮转）
├── frontend/
│   ├── index.html           # 控制台风格单页界面
│   ├── styles.css           # 示波器美学（深色 + 荧光青绿/品红）
│   └── app.js               # WebSocket 事件处理 + 进度/诊断/重试 + 示波器动画
├── scripts/
│   ├── install.sh           # 一键安装
│   ├── run.sh               # 启动服务
│   ├── launch_chrome.sh     # 启动带调试端口的 Chrome（用于网页 AI 登录）
│   └── logic_setup.scpt     # Logic Pro 环境准备（macOS）
├── tests/                   # 单元测试（乐理 / JSON 提取 / MIDI / 配置 / 诊断 / 任务追踪）
├── config/
│   └── config.example.yaml  # 配置模板
├── .github/workflows/ci.yml # GitHub Actions：push/PR 自动跑测试
├── Dockerfile               # 非 macOS 开发/测试镜像
├── requirements.txt
└── README.md
```

### 工作流

1. 用 `launch_chrome.sh` 启动一个带调试端口的 Chrome，在里面登录你的网页 AI（豆包/Kimi/千问…）。
2. 启动后端，在前端「设置」里选好网页 AI 服务并点「测试连接」。
3. 在控制台网页输入创作指令（如「一首 100BPM 的 A 小调抒情流行」）。
4. 后端 `WebAIDriver` 把指令注入网页 AI 对话框，等待流式回复完成，抓取文本并解析为 JSON（`StageResult`）。
5. `Commander` 把 `StageResult` 交给 `DAWController`：
   - `MidiEngine` 生成标准 MIDI 文件；
   - `AppleScriptBridge` 通过 osascript 在 Logic Pro 里建轨道、导入 MIDI、调混音器、挂插件、Bounce。
6. 全程事件经 WebSocket 实时推给前端，日志/轨道/混音台/导出状态即时更新。

> **未装 Playwright / 未连接浏览器也能跑**：自动降级到内置 demo 生成器，产出完整可执行的四个阶段（A 小调示范曲），方便在无浏览器/非 macOS 环境下演示与开发。

## 🚀 快速开始

### 1. 克隆与安装

```bash
git clone https://github.com/zjj9994/ai-daw-conductor.git
cd ai-daw-conductor
./scripts/install.sh        # 创建 venv、装依赖（含 Playwright）、生成 config.yaml
```

### 2. 登录网页 AI（一次性）

```bash
./scripts/launch_chrome.sh   # 启动带调试端口的 Chrome 并打开豆包
```

在弹出的 Chrome 里**登录你的网页 AI 账号**（豆包 / Kimi / 通义千问 / 智谱清言均可），登录状态会被这个独立用户目录记住。之后保持该 Chrome 开着即可。

> 也可改用 `config.yaml` 里的 `browser.mode: persistent`，由后端直接启动一个 Chromium 并保留登录态，省去手动开 Chrome（首次仍需手动登录一次）。

### 3.（macOS）准备 Logic Pro

```bash
osascript scripts/logic_setup.scpt
```

按提示在 Logic Pro 里：开启高级工具、启用「AI-DAW-Conductor」虚拟 MIDI 输入、授予终端辅助功能权限。

### 4. 启动并使用

```bash
./scripts/run.sh
# 打开 http://127.0.0.1:8787
```

点右上「设置」选择网页 AI 服务 → 「测试连接」确认已连上 → 输入创作要求 → 选「全流程」或单阶段 → 「开始制作」。所有进度、日志、轨道、混音与导出状态实时显示。

## 🔌 API 速览

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | `/` | 前端页面 |
| GET  | `/api/health` | 健康检查 / 网页 AI 是否就绪 / 浏览器是否已连接 |
| GET  | `/api/diagnostics` | 各子系统诊断（Playwright/CDP/MIDI/AppleScript）+ 建议 |
| GET/POST | `/api/settings` | 读取/更新网页 AI 与浏览器配置 |
| POST | `/api/browser/connect` | 连接/复用已登录的网页 AI 标签页 |
| POST | `/api/stage` | 执行单阶段（compose/arrange/mix/master） |
| POST | `/api/pipeline` | 执行完整四阶段流水线 |
| POST | `/api/autonomous` | 自主制作：AI 不间断完成整首作品（含自评估+自动重连） |
| GET  | `/api/task/status` | 当前任务状态与进度（供轮询） |
| POST | `/api/cancel` | 取消当前任务 |
| GET  | `/api/renders` | 渲染历史（导出文件列表，重启后仍保留） |
| DELETE | `/api/renders` | 清空渲染历史记录（不删磁盘文件） |
| GET  | `/api/renders/{idx}/download` | 下载渲染历史中指定导出文件 |
| GET  | `/api/midis` | 列出已生成的 MIDI 文件 |
| GET  | `/api/midis/download?path=` | 下载指定 MIDI 文件（路径需在 render_dir 下） |
| WS   | `/ws` | 实时事件流（日志/进度/轨道/混音/导出） |

## ⚙️ 配置项

见 [config/config.example.yaml](config/config.example.yaml)。关键项：

- `ai.provider`：网页 AI 服务（`doubao` / `kimi` / `qwen` / `zhipu` / `custom`）
- `ai.web_url`：网页 AI 聊天页地址（留空用内置默认）
- `ai.selectors.*`：网页 DOM 选择器（一般无需改；页面改版导致抓取失败时可调）
- `browser.mode`：`cdp`（连接已登录 Chrome，推荐）或 `persistent`（独立 Chromium 目录）
- `browser.cdp_url`：CDP 调试地址（默认 `http://127.0.0.1:9222`）
- `daw.midi_port` / `daw.use_applescript` / `daw.render_dir`
- `workflow.pipeline`：流水线阶段顺序

环境变量覆盖：`AI_PROVIDER` / `AI_WEB_URL` / `BROWSER_MODE` / `BROWSER_CDP_URL` / `BROWSER_USER_DATA_DIR` / `SERVER_PORT`。

### 网页 AI 选择器说明

各网页 AI 的 DOM 不一且会改版。驱动器做了多策略兜底：输入框优先找 `textarea`/`[contenteditable]`，发送优先配置的发送按钮、否则按回车；助手回复优先用配置的 `response` 选择器、否则用 JS 抓取最后一条消息容器；「停止生成」状态用 `generating` 选择器或文本稳定性判断。若某家网页 AI 改版导致抓取异常，在 `config.yaml` 的 `ai.selectors` 里填入对应选择器即可。

## 🩺 系统诊断

前端右上角「诊断」按钮（或 `GET /api/diagnostics`）会逐项检查：

| 检查项 | 含义 | 不通过时的处理 |
|--------|------|---------------|
| Playwright | 浏览器自动化库是否安装 | `pip install playwright` |
| CDP 端口 | 调试 Chrome 是否可达 | 运行 `./scripts/launch_chrome.sh` 并登录网页 AI |
| MIDO | MIDI 文件生成库 | `pip install mido` |
| python-rtmidi | 实时 MIDI 虚拟端口（可选） | `pip install python-rtmidi`（不影响文件模式） |
| AppleScript | Logic Pro 实控（仅 macOS） | 非 macOS 自动降级为模拟模式 |

诊断结果会附上针对性建议。出错后修复对应项，刷新页面再点「诊断」复查即可。前端会自动轮询 `/api/task/status`（每 1.5s）更新进度条与阶段状态；某阶段失败时会高亮为红色并显示「重试阶段」按钮，点击即可用同一指令重跑该阶段。

### 渲染历史与下载

每次导出（Bounce）都会记入渲染历史，并**持久化到 `~/.ai-daw-conductor/render_history.json`**，重启服务后仍可查看。前端「渲染历史」面板里每条记录右侧有下载按钮（`GET /api/renders/{idx}/download`），点「清空」可清除记录（不删磁盘文件）。生成的 MIDI 文件存放在 `daw.render_dir`，可通过 `GET /api/midis` 列出、`GET /api/midis/download?path=` 下载（路径必须在 render_dir 下，防越权）。

### 创作指令模板

指令输入框下方有「模板」栏：点「存为模板」把当前指令存入浏览器本地存储，之后点模板名即可一键填入，点「×」删除。模板只在当前浏览器保留，不上传服务端。

## 🤖 自主制作模式

点指令栏的「自主制作」按钮，AI 会**不间断、自主地**完成一整首音乐/歌曲，全程无需用户介入。这是本项目的核心能力，与普通「全流程」模式的区别：

| 能力 | 全流程 `/api/pipeline` | 自主制作 `/api/autonomous` |
|------|------------------------|----------------------------|
| 阶段衔接 | 线性跑完四阶段 | 自动衔接，上下文累积传递 |
| 整首连贯性 | 各阶段独立决策 | compose 确定的标题/调性/速度/段落结构/轨道命名被后续阶段严格延续 |
| 质量保障 | 无 | 每阶段产出后 AI 自评估，不达标带反馈重做（默认最多 2 次） |
| 断线恢复 | 无 | 每阶段前做网页 AI 健康检查，断连自动重连 |
| 失败处理 | 单阶段失败即终止 | 单阶段彻底失败降级为 demo，流水线继续推进直至完成 |
| 取消 | 支持 | 支持（信号立即传播，下一检查点中断） |

### 工作流程

```
用户输入「一首 100BPM A 小调抒情流行…」并点「自主制作」
  │
  ├─ [1/4] 作曲 compose
  │     AI 确定标题/调性/速度/段落结构，生成主旋律与和声
  │     → 自评估 → 通过则进入下一阶段，不达标带反馈重做
  │     → 健康检查网页 AI 是否仍在线
  │
  ├─ [2/4] 编曲 arrange
  │     延续作曲的结构，新增鼓组/贝斯/铺底/副旋律
  │     → 自评估 → 重做（若需要）
  │
  ├─ [3/4] 混音 mix
  │     覆盖全部已有轨道的音量/声相/插件/发送
  │     → 自评估 → 重做（若需要）
  │
  └─ [4/4] 母带 master
        主输出 EQ→压缩→限制器，导出 wav 成品
        → 自评估 → 重做（若需要）
  │
  └─ 完成：整首作品导出，渲染历史记录，前端展示
```

### 自评估开关

指令栏右侧的「自评估」复选框控制是否启用 AI 自评。关闭后等同线性推进（速度更快但无质量兜底）。开启时每阶段会多一次 AI 对话用于质检，长任务更耗时但产出更可靠。

### API 调用

```bash
curl -X POST http://127.0.0.1:8787/api/autonomous \
  -H "Content-Type: application/json" \
  -d '{"prompt":"一首 100BPM 的 A 小调抒情流行，钢琴主导，副歌加入弦乐与鼓组","enable_self_eval":true,"max_stage_retries":2}'
```

随时可 `POST /api/cancel` 中断。进度通过 `GET /api/task/status`（mode 字段为 `autonomous`）或 WebSocket 实时查看。

### 注意事项

- 自主模式对话次数较多（每阶段 1 次生成 + 1 次评估 + 可能的重做），网页 AI 若有频率限制需留意；
- 网页 AI 标签页需保持登录且不被手动关闭，断连后系统会自动重连；
- 非 macOS 环境下 Logic Pro 操作为模拟模式，但 MIDI 生成、自评估、流水线编排均正常工作。

## 🛠️ 故障排查

**「demo 模式」一直不消失 / 测试连接失败**
- 确认已 `pip install playwright` 且 `playwright install chromium`；
- 确认用 `./scripts/launch_chrome.sh`（而非普通 Chrome）启动了带调试端口的 Chrome，并在里面登录了网页 AI；
- 点「诊断」看 CDP 端口是否可达；容器内连宿主机 Chrome 时把 `cdp_url` 设为 `http://host.docker.internal:9222`，并让 Chrome 监听 `0.0.0.0`。

**网页 AI 回复抓不到 / 一直等待超时**
- 多数是网页 AI 改版导致选择器失效。打开开发者工具找到输入框/发送按钮/回复容器的真实选择器，填入 `config.yaml` 的 `ai.selectors`；
- 驱动器会在超时前保存截图到 `~/.ai-daw-conductor/screenshots/`，便于排查；
- 适当调大 `ai.timeout`（默认 180s，长回复可能需要更长）；
- 确认网页 AI 没有弹出验证码/登录失效弹窗。

**AI 回复了但解析 JSON 失败**
- 驱动器会自动剥离 `<think>` 标签、代码围栏、尾随逗号；若仍失败，换一家网页 AI（Kimi/Qwen 通常更稳定输出 JSON）或在指令里强调「只输出 JSON，不要任何解释」。

**MIDI 生成了但 Logic Pro 没导入 / 混音器没反应**
- 仅 macOS + Logic Pro 支持自动导入与混音器操作，其他平台为模拟模式（只生成 MIDI 文件）；
- macOS 下确认已授予终端「辅助功能」权限（系统设置 → 隐私与安全性 → 辅助功能）；
- 运行 `osascript scripts/logic_setup.scpt` 检查 Logic Pro 环境。

**导出（Bounce）没生成文件**
- macOS 下 Bounce 由 AppleScript 触发，需 Logic Pro 处于打开状态且有活动项目；
- 非 macOS 平台会生成 0 字节占位文件，属正常现象；
- 检查 `daw.render_dir` 是否有写入权限。

**WebSocket 日志不刷新**
- 前端会自动每 2s 重连；若仍不行，检查 `SERVER_PORT` 是否被占用、防火墙是否放行；
- 浏览器控制台看 `ws` 连接错误。

## 🐳 Docker（非 macOS 体验 / 测试）

容器内可运行 FastAPI 服务、Playwright 网页 AI 驱动与 MIDI 生成；Logic Pro 实控与实时 MIDI 输出会自动降级为模拟模式。推荐用 CDP 连接宿主机上已登录网页 AI 的 Chrome。

```bash
# 1. 宿主机启动带调试端口的 Chrome 并登录网页 AI（监听 0.0.0.0 以便容器访问）
google-chrome --remote-debugging-port=9222 --remote-debugging-address=0.0.0.0

# 2. 构建镜像
docker build -t ai-daw-conductor .

# 3. 运行（CDP 指向宿主机）
docker run -p 8787:8787 \
  -e BROWSER_CDP_URL=http://host.docker.internal:9222 \
  ai-daw-conductor

# 打开 http://127.0.0.1:8787
```

也可挂载自定义配置：`-v $(pwd)/config/config.yaml:/app/config/config.yaml:ro`。

## 🎛️ 平台说明

| 平台 | 网页 AI 驱动 | MIDI 生成 | Logic Pro 实控 |
|------|-------------|-----------|---------------|
| **macOS**（推荐） | ✅ Playwright | ✅ 文件 + 虚拟端口实时 | ✅ AppleScript 全功能 |
| Linux / Windows | ✅ Playwright | ✅ 文件生成 | ⚠️ 模拟模式（仅生成 MIDI，不自动导入/导出） |

非 macOS 平台仍可完整体验 AI 决策与 MIDI 文件生成；最终的轨道创建、混音器操作与 Bounce 需要 macOS 上的 Logic Pro。

## 🔐 安全与合规

- 不申请、不存储任何大模型 API Key；复用你本人已登录的网页 AI 会话。
- `config/config.yaml` 与浏览器用户目录已被 `.gitignore` 忽略，不会入库。
- 服务默认仅监听 `127.0.0.1`，不对外暴露；CDP 调试端口也仅本机。
- 请遵守所使用网页 AI 的服务条款，自担使用风险。

## 📝 许可证

MIT — 见 [LICENSE](LICENSE)。

## 🙏 致谢

- [豆包](https://www.doubao.com) / [Kimi](https://kimi.moonshot.cn) / [通义千问](https://tongyi.aliyun.com) / [智谱清言](https://chatglm.cn) — 网页端 AI
- [Logic Pro](https://www.apple.com/logic-pro/) — 数字音频工作站
- [Playwright](https://playwright.dev) — 浏览器自动化
- [mido](https://mido.readthedocs.io/) / [python-rtmidi](https://spoti9.github.io/rtmidi/) — MIDI 处理
- [FastAPI](https://fastapi.tiangolo.com/) — 后端框架
