# 🎹 AI-DAW-Conductor

> 用网页端 AI 模型（豆包 / 火山方舟 Ark）指挥 Logic Pro，让 AI **独立完成** 作曲 → 编曲 → 混音 → 母带 全流程。

在浏览器里用自然语言描述你想要的歌曲，AI 负责所有音乐创作决策，后端把这些决策翻译成 MIDI、轨道、混音参数与插件操作，自动驱动 Logic Pro 完成制作并导出母带。

```
┌────────────┐    WebSocket     ┌─────────────────┐    MIDI + AppleScript    ┌───────────┐
│  网页前端   │ ◄──────────────► │  本地后端服务     │ ───────────────────────► │ Logic Pro  │
│ (浏览器)    │                  │  FastAPI + WS   │                          │  (macOS)   │
└────────────┘                  └────────┬────────┘                          └───────────┘
                                          │ 调用 LLM
                                          ▼
                                ┌─────────────────┐
                                │ 豆包 / 方舟 Ark  │  ← OpenAI 兼容接口
                                └─────────────────┘
```

## ✨ 能做什么

AI 在四个阶段独立完成决策与执行：

| 阶段 | AI 决策 | 对 Logic Pro 的操作 |
|------|---------|--------------------|
| **作曲 Compose** | 标题、风格、速度、调性、段落结构、主旋律与和弦 | 新建项目、建软件乐器轨、生成 MIDI 并导入 |
| **编曲 Arrange** | 鼓组/贝斯/和声铺底/副旋律配置与各声部音符 | 新增轨道、按段落写入 MIDI 片段 |
| **混音 Mix** | 音量、声相、EQ、压缩、发送、效果链 | 设置混音器参数、挂插件、建 Reverb Bus |
| **母带 Master** | 母带链（EQ→压缩→限制器）与导出参数 | 主输出挂母带插件、Bounce 导出 wav/aiff/mp3 |

## 🧱 架构

```
ai-daw-conductor/
├── backend/
│   ├── server.py            # FastAPI + WebSocket，前端入口与事件流
│   ├── ai_engine.py         # 调用豆包/Ark（OpenAI 兼容），产出 StageResult；含离线 demo 生成器
│   ├── commander.py         # 把 AI 决策编排为有序执行（流水线）
│   ├── daw_controller.py    # 高层动作：建项目/轨道/MIDI/混音/母带/导出
│   ├── midi_engine.py       # 生成标准 MIDI 文件 + 虚拟端口实时输出（mido/rtmidi）
│   ├── applescript_bridge.py# osascript 控制 Logic Pro（轨道/混音器/插件/Bounce）
│   ├── music_theory.py      # 音名互转、音阶、和弦、量化等乐理工具
│   ├── models.py            # pydantic 数据模型（StageResult / TrackSpec / MixParams ...）
│   └── config_loader.py     # 合并 config.yaml 与环境变量
├── frontend/
│   ├── index.html           # 控制台风格单页界面
│   ├── styles.css           # 示波器美学（深色 + 荧光青绿/品红）
│   └── app.js               # WebSocket 事件处理 + 示波器动画
├── scripts/
│   ├── install.sh           # 一键安装
│   ├── run.sh               # 启动服务
│   └── logic_setup.scpt     # Logic Pro 环境准备（macOS）
├── config/
│   └── config.example.yaml  # 配置模板
├── requirements.txt
└── README.md
```

### 工作流

1. 用户在网页输入创作指令（如「一首 100BPM 的 A 小调抒情流行」）。
2. 后端 `AIEngine` 调用豆包模型，每个阶段返回严格 JSON（`StageResult`）。
3. `Commander` 把 `StageResult` 交给 `DAWController`：
   - `MidiEngine` 生成标准 MIDI 文件；
   - `AppleScriptBridge` 通过 osascript 在 Logic Pro 里建轨道、导入 MIDI、调混音器、挂插件、Bounce。
4. 全程事件经 WebSocket 实时推给前端，日志/轨道/混音台/导出状态即时更新。

> **无需 API Key 也能跑**：未配置凭证时，`AIEngine` 自动降级到内置 demo 生成器，产出完整可执行的四个阶段（A 小调示范曲），方便在没有密钥/非 macOS 环境下演示与开发。

## 🚀 快速开始

### 1. 克隆与安装

```bash
git clone https://github.com/zjj9994/ai-daw-conductor.git
cd ai-daw-conductor
./scripts/install.sh        # 创建 venv、装依赖、生成 config.yaml
```

### 2. 配置 AI（豆包 / 火山方舟 Ark）

编辑 `config/config.yaml`，填入：

```yaml
ai:
  provider: doubao
  base_url: "https://ark.cn-beijing.volces.com/api/v3"
  api_key: "你的 Ark API Key"          # console.volcengine.com/ark 获取
  model: "ep-xxxxxxxxxxxxxxxx"          # 豆包推理接入点 id，或 doubao-pro-32k 等
```

也可以启动后在网页「设置」里填写并即时生效。

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

在网页里输入你的创作要求，选择「全流程」或单个阶段，点击「开始制作」即可。所有进度、日志、轨道、混音与导出状态会实时显示。

## 🔌 API 速览

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | `/` | 前端页面 |
| GET  | `/api/health` | 健康检查 / AI 是否在线 |
| GET/POST | `/api/settings` | 读取/更新 AI 配置 |
| POST | `/api/stage` | 执行单阶段（compose/arrange/mix/master） |
| POST | `/api/pipeline` | 执行完整四阶段流水线 |
| POST | `/api/cancel` | 取消当前任务 |
| WS   | `/ws` | 实时事件流（日志/进度/轨道/混音/导出） |

## ⚙️ 配置项

见 [config/config.example.yaml](config/config.example.yaml)。关键项：

- `ai.*`：模型 provider / base_url / api_key / model / temperature
- `daw.midi_port`：虚拟 MIDI 端口名（Logic Pro 输入端需匹配）
- `daw.use_applescript`：是否用 AppleScript 实控（仅 macOS）
- `daw.render_dir`：导出目录
- `workflow.pipeline`：流水线阶段顺序

环境变量覆盖：`AI_API_KEY` / `AI_BASE_URL` / `AI_MODEL` / `AI_PROVIDER` / `SERVER_PORT`。

## 🎛️ 平台说明

| 平台 | AI 决策 | MIDI 生成 | Logic Pro 实控 |
|------|---------|-----------|---------------|
| **macOS**（推荐） | ✅ 豆包 | ✅ 文件 + 虚拟端口实时 | ✅ AppleScript 全功能 |
| Linux / Windows | ✅ 豆包 | ✅ 文件生成 | ⚠️ 模拟模式（仅生成 MIDI，不自动导入/导出） |

非 macOS 平台仍可完整体验 AI 决策与 MIDI 文件生成，便于开发与演示；最终的轨道创建、混音器操作与 Bounce 需要 macOS 上的 Logic Pro。

## 🔐 安全

- `config/config.yaml` 与 `.env` 已被 `.gitignore` 忽略，密钥不会入库。
- API Key 仅存在本机内存与本地配置，前端展示时脱敏。
- 服务默认仅监听 `127.0.0.1`，不对外暴露。

## 📝 许可证

MIT — 见 [LICENSE](LICENSE)。

## 🙏 致谢

- [豆包 / 火山方舟 Ark](https://www.volcengine.com/product/ark) — 大语言模型
- [Logic Pro](https://www.apple.com/logic-pro/) — 数字音频工作站
- [mido](https://mido.readthedocs.io/) / [python-rtmidi](https://spoti9.github.io/rtmidi/) — MIDI 处理
- [FastAPI](https://fastapi.tiangolo.com/) — 后端框架
