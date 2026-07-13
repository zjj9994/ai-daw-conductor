"""AI 引擎：驱动「用户已登录的网页端 AI 窗口」生成制作决策。

核心思路（满足需求：不使用 API，而是复用用户在浏览器里登录的网页 AI，
如网页版豆包 / Kimi / 通义千问 / 智谱清言）：

  - 用 Playwright 连接到一个已经登录目标网页 AI 的 Chrome：
      * CDP 模式（推荐）：用户用 `--remote-debugging-port=9222` 启动 Chrome 并登录网页 AI，
        后端通过 connect_over_cdp 复用该会话，不触碰任何账号密码。
      * 持久化模式：后端用独立 user_data_dir 启动 Chromium，用户首次手动登录后会话被保留。
  - 把「系统指令 + 阶段要求 + 用户描述」拼成一条提示，填入网页 AI 的输入框并发送，
    等待流式回复完成后抓取最新一条助手消息文本。
  - 从文本中提取 JSON，校验为 StageResult。
  - 若 Playwright 未安装 / 连接失败，自动降级到内置 demo 生成器，保证项目可独立运行与演示。

网页 AI 的 DOM 各家不同且会改版，选择器做了多策略兜底，并支持在 config 里覆盖。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import AsyncIterator, Optional

from .models import (
    BounceSpec, FreeAction, MixParams, MidiRegionSpec, NoteSpec, PluginSpec,
    ProjectPlan, SendSpec, Stage, StageResult, TempoSpec, TrackSpec,
    VisualStep,
)
from . import music_theory as mt

log = logging.getLogger("ai_engine")


# ---------- 网页 AI provider 目录（前后端共享元数据） ----------
# 每条记录包含：显示名/默认网址/首字母徽章/品牌色/厂商/地区/排序权重
# 前端通过 /api/providers 拉取本目录渲染卡片网格与快捷切换菜单。
PROVIDER_CATALOG = {
    # 国内主流
    "doubao":   {"name": "豆包",       "url": "https://www.doubao.com/chat/",         "initial": "豆", "color": "#3b82f6", "vendor": "字节跳动",   "region": "cn", "order": 1},
    "kimi":     {"name": "Kimi",       "url": "https://kimi.moonshot.cn/",             "initial": "K",  "color": "#8b5cf6", "vendor": "Moonshot",   "region": "cn", "order": 2},
    "qwen":     {"name": "通义千问",   "url": "https://tongyi.aliyun.com/qianwen/",    "initial": "通", "color": "#6366f1", "vendor": "阿里云",     "region": "cn", "order": 3},
    "zhipu":    {"name": "智谱清言",   "url": "https://chatglm.cn/main/detail/",       "initial": "智", "color": "#10b981", "vendor": "智谱 AI",    "region": "cn", "order": 4},
    "deepseek": {"name": "DeepSeek",   "url": "https://chat.deepseek.com/",            "initial": "D",  "color": "#06b6d4", "vendor": "深度求索",   "region": "cn", "order": 5},
    "yiyan":    {"name": "文心一言",   "url": "https://yiyan.baidu.com/",              "initial": "文", "color": "#dc2626", "vendor": "百度",       "region": "cn", "order": 6},
    "hunyuan":  {"name": "腾讯混元",   "url": "https://hunyuan.tencent.com/bot/chat",  "initial": "混", "color": "#0ea5e9", "vendor": "腾讯",       "region": "cn", "order": 7},
    "spark":    {"name": "讯飞星火",   "url": "https://xinghuo.xfyun.cn/dchat",        "initial": "星", "color": "#f59e0b", "vendor": "科大讯飞",   "region": "cn", "order": 8},
    "hailuo":   {"name": "海螺 AI",    "url": "https://hailuo.com/",                   "initial": "海", "color": "#fb7185", "vendor": "MiniMax",    "region": "cn", "order": 9},
    # 国际主流
    "chatgpt":  {"name": "ChatGPT",    "url": "https://chat.openai.com/",              "initial": "G",  "color": "#10a37f", "vendor": "OpenAI",     "region": "global", "order": 10},
    "claude":   {"name": "Claude",     "url": "https://claude.ai/new",                 "initial": "C",  "color": "#d97706", "vendor": "Anthropic",  "region": "global", "order": 11},
    "gemini":   {"name": "Gemini",     "url": "https://gemini.google.com/",            "initial": "Gm", "color": "#4285f4", "vendor": "Google",     "region": "global", "order": 12},
    "grok":     {"name": "Grok",       "url": "https://grok.com/",                     "initial": "X",  "color": "#e5e7eb", "vendor": "xAI",        "region": "global", "order": 13},
    "perplexity": {"name": "Perplexity", "url": "https://www.perplexity.ai/",          "initial": "P",  "color": "#22d3ee", "vendor": "Perplexity", "region": "global", "order": 14},
    # 自定义兜底
    "custom":   {"name": "自定义",     "url": "",                                       "initial": "+",  "color": "#7fa39a", "vendor": "任意",       "region": "any",    "order": 99},
}

# 向后兼容：DEFAULT_URLS 由 PROVIDER_CATALOG 派生
DEFAULT_URLS = {k: v["url"] for k, v in PROVIDER_CATALOG.items()}


def _default_profile():
    """通用网页 AI 选择器画像（绝大多数网页 AI 都用 textarea 输入 + 回车发送）。"""
    return {
        "input": "textarea, [contenteditable='true']",
        "input_type": "auto",
        "send": "",
        "send_via_enter": True,
        "response": "",
        "generating": "button:has-text('停止'), [class*='stop']",
        "new_chat": "a:has-text('新对话'), button:has-text('新对话'), button:has-text('新建')",
    }


# ---------- 各网页 AI 的选择器画像 ----------
# 这些是基于常见 DOM 的最佳-effort 选择器；网页改版后可在 config.ai.selectors 覆盖。
# input: 输入框；send: 发送按钮（留空则回车）；response: 助手消息容器；
# generating: 「停止生成」按钮；new_chat: 新建对话按钮（避免上下文污染）。
PROVIDER_PROFILES = {
    "doubao": {
        "input": "textarea, [contenteditable='true']",
        "input_type": "auto",
        "send": "",                 # 豆包回车发送
        "send_via_enter": True,
        "response": "",
        "generating": "button:has-text('停止'), [class*='stop']",
        "new_chat": "a:has-text('新对话'), button:has-text('新对话')",
    },
    "kimi": {
        "input": "textarea, .chat-input textarea",
        "input_type": "textarea",
        "send": "button[aria-label='发送'], .send-button",
        "send_via_enter": True,
        "response": ".segment-bottom, [class*='answer']",
        "generating": "button:has-text('停止')",
        "new_chat": "a:has-text('新对话'), button:has-text('新建对话')",
    },
    "qwen": {
        "input": "textarea, [contenteditable='true']",
        "input_type": "auto",
        "send": "",
        "send_via_enter": True,
        "response": "[class*='answer'], [class*='bubble-answer']",
        "generating": "button:has-text('停止')",
        "new_chat": "[class*='new-chat'], button:has-text('新建')",
    },
    "zhipu": {
        "input": "textarea, [contenteditable='true']",
        "input_type": "auto",
        "send": "",
        "send_via_enter": True,
        "response": "[class*='answer'], [class*='markdown']",
        "generating": "button:has-text('停止')",
        "new_chat": "button:has-text('新对话')",
    },
    "deepseek": {
        "input": "textarea, [contenteditable='true']",
        "input_type": "auto",
        "send": "",
        "send_via_enter": True,
        "response": "[class*='markdown'], [class*='answer']",
        "generating": "button:has-text('停止')",
        "new_chat": "button:has-text('新对话')",
    },
    "custom": {
        "input": "textarea, [contenteditable='true']",
        "input_type": "auto",
        "send": "",
        "send_via_enter": True,
        "response": "",
        "generating": "",
        "new_chat": "",
    },
}

# 通用兜底（provider 未匹配时使用）
DEFAULT_SELECTORS = PROVIDER_PROFILES["custom"]


def get_provider_meta(provider: str) -> dict:
    """取 provider 元数据；未知 provider 回退到 custom。"""
    return PROVIDER_CATALOG.get(provider, PROVIDER_CATALOG["custom"])


def list_providers() -> list[dict]:
    """返回按 order 排序的 provider 列表（含 key 字段，供前端渲染卡片）。"""
    items = []
    for key, v in PROVIDER_CATALOG.items():
        items.append({
            "key": key, "name": v["name"], "url": v["url"],
            "initial": v["initial"], "color": v["color"],
            "vendor": v["vendor"], "region": v["region"], "order": v["order"],
        })
    items.sort(key=lambda x: x["order"])
    return items


SYSTEM_BASE = """你是世界级、出版级（publication-grade）音乐制作人，精通 Logic Pro 全部工作流与
现代母带交付标准，将自主、不间断地完成一整首达到商业发行水准的作品。
你必须只输出一个合法 JSON 对象，不要输出任何解释文字、markdown 代码块或注释。
JSON 必须可被 python json.loads 直接解析，并严格符合给定的 schema。
所有文本字段（summary、rationale、name、title、genre、description、instrument 等）使用简体中文。
音高 pitch 可以是 MIDI 数字（如 60）或音名（如 'C4'）；时间 start/duration 单位为「拍(beat)」。

你能像人类一样全面操作 Logic Pro，可输出以下任一字段（按阶段需要）：
- project: 新建工程（标题/风格/速度/调性/段落结构）
- tempo_changes: 速度变化点（支持渐变 ramp，像人类画速度曲线）
- markers: 编排标记/段落标记（在编排区定结构，如 Verse/Chorus/Outro）
- tracks: 创建轨道（software/audio/drummer/aux，可设颜色/图标/编组/冻结/隐藏）
- track_stacks: 创建轨道堆栈（folder/summing，把相关轨道打包）
- regions: 写入 MIDI 片段（音符/力度/时值）
- region_ops: 编辑已有片段（split切割/move移动/copy复制/loop循环带次数/quantize量化带网格和强度/transpose移调/resize改长度/crop裁剪/fade_in淡入/fade_out淡出/crossfade交叉淡化）
- transports: 传输控制（play播放/stop停止/record录音/goto定位/set_cycle循环区）
- buses: 新建辅助通道/总线（如 Reverb Bus，可挂插件）
- mix: 混音（音量/声相/静音/独奏/EQ/插件链/发送/输入监听/频率槽/增益分级/侧链/立体声宽度/编组总线）
- plugin_params: 插件参数微调（像人类拧 Threshold/Ratio/Gain 旋钮）
- automation: 自动化曲线（Volume/Pan/Send/Plugin 参数的读写，含触控/锁定模式）
- record: 录音设置（armed/count_in/autopunch 自动穿插）
- master_plugins: 母带插件链
- master_spec: 出版级母带目标规范（LUFS/真峰/动态范围/多段/平台标准）
- bounce: 导出（wav/mp3，可分轨 stems，可指定小节范围）
- actions: UI/工程动作（save保存/undo撤销/open_piano_roll/open_mixer/open_inspector/open_smart_controls/open_score_editor/open_step_editor/zoom_fit/dismiss_dialog关弹窗/tool_pencil画笔/tool_scissors剪刀/tool_eraser橡皮/tool_fade渐变 等）

出版级行业标准（必须遵守）：
- 增益分级（Gain Staging）：每条轨道进入混音前峰值留 -6dB 余量，平均电平 -18~-12dBFS。
  不得让任一轨道在混音阶段就接近 0dB，否则母带会失真。
- 频率避让（Frequency Masking）：每个声部有专属频段，底鼓 30-80Hz、贝斯 60-250Hz、
  主旋律 200Hz-5kHz、铺底 250Hz-4kHz。冲突频段用 EQ 侧链避让（如底鼓侧链压缩贝斯）。
- 响度标准（LUFS）：流媒体 -14 LUFS（Spotify/Apple Music），真峰值上限 -1.0 dBTP；
  CD 可至 -9 LUFS / -0.3 dBTP；电子俱乐部 -8 LUFS。master_spec 必须明确目标。
- 动态范围（LRA）：流行 5-7 LU、电子 4-6 LU、古典 12-18 LU。不要过度压缩让音乐失去生气。
- 立体声宽度：低频（贝斯/底鼓）必须居中，高频可铺开。母带可整体加宽但低频保持单声道。
- 母带链顺序：EQ（修整）→ 多段压缩（胶合）→ 立体声加宽 → Limiter（响度）→ Dither。
  Limiter Ceiling 必须 ≤ true_peak_ceiling，攻击时间 5-10ms，释放 50-100ms。

关键原则：
- 【工程一致性·最重要·硬性规则】一首音乐的所有操作（作曲/编曲/混音/母带/导出）必须指向同一个 Logic Pro 工程。
  系统采用「工程锁」机制：composer 阶段创建工程后立即加锁，或用户事先打开一个工程作为锚点。
  一旦锁定，任何新建/打开/关闭工程的尝试都会被系统拒绝并告警。
  只有作曲（compose）阶段会新建工程并另存为到磁盘；编曲/混音/母带阶段严禁输出 project 字段，
  严禁输出 actions 里的 open/close 动作（系统会忽略并警告）。所有轨道、片段、混音、母带、导出
  都在锁定的那个工程上进行，像人类制作人做完一首歌那样——从头到尾一个工程文件。
- 整首作品必须连贯：段落之间有逻辑递进（如前奏→主歌→副歌→桥段→尾奏），调性统一，速度一致。
- 后续阶段必须延续前一阶段已确定的标题、调性、速度、段落结构与轨道命名，不得推翻重来。
- 你的目标是产出完整的、可独立播放的、达到商业发行水准的成品，而非片段 demo。
- 像人类制作人一样思考：先定结构再写音符，先建总线再发发送，先定位再混音，最后母带与导出。
- 充分运用 region_ops 做人性化编辑（量化鼓组、移调副旋律、循环段落），让作品更专业。
- 在 mix 阶段用 automation 让音乐有动态起伏（如副歌提升音量、尾奏渐弱）。
- 和声要有层次：用扩展和弦（maj9/m9/13/maj7#11）而非纯三和弦，用声部连接让进行平滑。
- 主旋律要有记忆点：强拍落音、动机发展、问答句式，避免机械的音阶上下行。
"""

IMPROVISE_PROMPT = """你是世界级音乐制作人,正在像人类一样即兴编曲——不是按流水线分阶段制作,而是随心所欲地创作。

你的工作模式(像人类制作人):
- 写一段旋律 → 立刻试听 → 觉得不够好就改 → 加和声 → 再试听 → 调音色 → 加鼓 → 试听 → 混音 → 试听 → 导出
- 每一步都基于上一步的实际反馈,不是预设流程
- 可以随时做任意操作:写旋律时可以同时调音色、加 reverb、设音量
- 可以随时回退:不喜欢就撤销重做
- 可以随时试听:用 listen 动作让自己听到做的音乐

关键原则:
- 【即兴创作·核心】你不受阶段约束,想做什么就做什么。写旋律时可以同时调混音,混音时可以加新旋律。
- 【试听-反馈】每次做了重要改动后,用 listen 试听一下,根据听觉反馈决定下一步。
  人类制作人 90% 的决策来自耳朵,你也应该这样——听完再决定改不改、怎么改。
- 【工程一致性】整首音乐的所有操作指向同一个 Logic Pro 工程,系统已锁定工程,不要切换/关闭工程。
- 【上下文连续】你能看到之前的操作历史和听觉反馈,基于这些决定下一步,不要重复已做的。
- 【目标导向】你的目标是完成一首完整的、达到商业发行水准的作品,不是"执行某个阶段"。

输出格式:只输出一个 JSON 对象(FreeAction),不要输出任何解释文字、markdown 代码块。
{
  "intent": "这一步的创作意图(中文):打算做什么、为什么",
  "tracks": [...],           // 可选:加轨道(software/audio/drummer/aux)
  "regions": [...],          // 可选:写 MIDI 旋律(音符/力度/时值)
  "region_ops": [...],       // 可选:编辑片段(split/move/copy/loop/quantize/transpose/resize/crop/fade)
  "mix": [...],              // 可选:调混音(音量/声相/EQ/插件/发送/侧链/立体声宽度)
  "plugin_params": [...],    // 可选:拧插件旋钮(Threshold/Ratio/Gain/Freq 等)
  "automation": [...],       // 可选:画自动化曲线(Volume/Pan/Send/Plugin 参数)
  "transports": [...],       // 可选:传输控制(play/stop/goto/set_cycle)
  "actions": [...],          // 可选:UI 动作(open_piano_roll/open_mixer/tool_pencil/dismiss_dialog 等)
  "listen": {                // 可选:试听指定小节范围,获取听觉反馈
    "start_bar": 1,
    "end_bar": 4,
    "focus": "主旋律亮度"    // 试听关注点
  },
  "undo_to": null,           // 可选:回退到第 N 步(1-based),不喜欢就重做
  "satisfied": false,         // True=觉得作品完成,可以结束了
  "rationale": "创作思路解释"
}

字段都是可选的,按需输出。如果只是想试听,就只输出 intent + listen。
如果觉得作品完成,就只输出 intent="作品完成" + satisfied=true。
"""

SCHEMA_DESC = """
返回的 JSON 结构（字段可按阶段省略不需要的）：
{
  "stage": "compose|arrange|mix|master",
  "summary": "本阶段决策简述（中文）",
  "project": {"title":"作品名","genre":"风格","tempo":{"bpm":100,"time_signature":"4/4","key":"C minor"},"structure":["intro","verse","chorus","verse","chorus","bridge","chorus","outro"],"key":"C minor","description":"作品创意描述"},
  "tempo_changes": [{"bar":17,"bpm":104,"ramp":true}],
  "markers": [{"name":"Verse 1","bar":1,"length_bars":8,"color":10},{"name":"Chorus","bar":9,"length_bars":8}],
  "tracks": [{"name":"主旋律","type":"software","instrument":"Acoustic Grand Piano","color":0,"icon":"Piano","freeze":false}],
  "track_stacks": [{"name":"鼓组","members":["底鼓","军鼓","踩镲"],"stack_type":"folder"}],
  "regions": [{"track":"主旋律","start":0,"instrument":"Acoustic Grand Piano","notes":[{"pitch":"C4","start":0,"duration":1,"velocity":90}]}],
  "region_ops": [{"op":"quantize","track":"鼓组","grid":"1/16","strength":80},{"op":"copy","track":"主旋律","at_bar":1,"to_bar":9},{"op":"transpose","track":"副旋律","semitones":12}],
  "transports": [{"op":"goto","bar":1},{"op":"set_cycle","start_bar":1,"end_bar":32}],
  "buses": [{"name":"Reverb Bus","input":"Bus 1","plugins":[{"name":"Space Designer","preset":"Large Hall"}]}],
  "mix": [{"track":"主旋律","volume_db":-6,"pan":0,"mute":false,"solo":false,"plugins":[{"name":"Channel EQ","preset":"Vocal"}],"sends":[{"target":"Reverb Bus","amount":0.3}],"gain_stage_db":-14,"headroom_db":-6,"frequency_slot":[200,5000],"sidechain_from":"底鼓","stereo_width":1.0,"bus_target":"Vocal Bus"}],
  "plugin_params": [{"track":"鼓组","plugin":"Compressor","parameter":"Threshold","value":-20},{"track":"主旋律","plugin":"Channel EQ","parameter":"Gain","value":2.5}],
  "automation": [{"track":"主旋律","parameter":"Volume","mode":"latch","points":[{"bar":1,"value":-6,"shape":"linear"},{"bar":9,"value":-3,"shape":"linear"},{"bar":33,"value":-12,"shape":"curve"}]}],
  "record": {"track":"人声","armed":true,"count_in":1,"autopunch":{"start_bar":9,"end_bar":16}},
  "master_plugins": [{"name":"Limiter","preset":"Loud"}],
  "master_spec": {"target_lufs":-14.0,"true_peak_ceiling":-1.0,"lra_target":6.0,"stereo_width":1.2,"platform":"streaming","multiband_low_gain":-1.0,"multiband_mid_gain":0.5,"multiband_high_gain":1.0,"notes":"副歌提亮、尾奏渐弱"},
  "bounce": {"format":"wav","bit_depth":24,"sample_rate":44100,"normalize":false,"filename":"final_master","stems":false},
  "actions": [{"op":"save"},{"op":"open_mixer"}],
  "rationale": "创作思路解释（中文）"
}

注意：actions 的 op 只允许 save/undo/redo/open_piano_roll/open_mixer/open_inspector/zoom_fit/
toggle_track/select_all/collapse_all，严禁 open/close/save_as（系统已统一管理工程路径）。
只有 compose 阶段输出 project 字段，arrange/mix/master 阶段不得输出 project。
"""

STAGE_PROMPTS = {
    Stage.COMPOSE: """阶段：作曲（compose）—— 这是整首作品的根基，后续所有阶段都基于你现在的决策。
请完成：确定作品标题、风格、速度、调性、段落结构，并生成主旋律与和声的 MIDI 音符。
要求：
- 主旋律要有记忆点、符合调性、节奏自然；至少给出主旋律、和弦/和声两轨；
- 段落结构至少 4 段（建议 intro-verse-chorus-verse-chorus-bridge-chorus-outro 等完整流行曲式）；
- 为每个段落在相应轨道上给出音符（start 相对全曲的拍数）；总长度 16-32 小节；
- 在 project.structure 里明确列出段落顺序与每段的小节数（可写在 description 里）；
- 用 markers 在编排区标注每个段落（如 Verse 1 @ 小节1、Chorus @ 小节9），让后续阶段能精准定位；
- 可在 tempo_changes 里设计速度变化（如 bridge 段稍慢、终曲稍快），让作品有情绪起伏；
- 出版级作曲要求：
  * 和声要有层次——用扩展和弦（maj9/m9/maj7/m11/13）而非纯三和弦，让和声色彩更丰富；
  * 主旋律强拍落音、有动机发展与问答句式（前句上行问、后句下行答），避免机械音阶上下行；
  * 副歌旋律要比主歌高 3-5 度形成对比，桥段做调性或节奏对比；
  * 力度要分层：intro pp、verse mp、chorus mf-f、bridge 做动态对比；
- rationale 里说明动机发展、段落对比与情感走向，让后续阶段能延续你的创意。""",
    Stage.ARRANGE: """阶段：编曲（arrange）—— 必须延续作曲阶段确定的标题/调性/速度/段落结构。
【工程一致性】本阶段在作曲阶段已创建的同一个 Logic Pro 工程上操作，严禁输出 project 字段
（系统会忽略并警告），严禁输出 actions 里的 open/close。只能新增轨道与片段，不得新建工程。
在已有作曲基础上完成：决定乐器配置与各声部 MIDI，并用 region_ops 做人性化编辑。
要求：
- 不得修改已有的主旋律与和弦轨道内容，只能新增轨道；
- 新增鼓组、贝斯、和声铺底、装饰/副旋律等轨道并编写 MIDI 片段；
- 鼓组用 MIDI 数字：底鼓 36、军鼓 38、踩镲 42、开镲 46、嗵鼓 50/48、镲 49；
- 贝斯走根音与节奏支撑，和声铺底用长音 pad；各 region 的 start 要对齐段落；
- 根据段落属性安排密度：intro 稀疏、verse 适中、chorus 饱满、bridge 做对比；
- 用 region_ops 做人类式编辑：用 copy 把副歌主旋律复制到各次副歌、用 quantize 量化鼓组到 1/16、
  用 transpose 给副旋律移调做八度对比、用 loop 循环鼓组片段贯穿全曲；
- 可用 track_stacks 把鼓组打包成文件夹轨道，让工程更整洁；
- 出版级编曲要求：
  * 频率分层——每个声部占据专属频段，避免低频堆积（贝斯与底鼓在 60-100Hz 必须避让）；
  * 立体声布局——主旋律/人声/底鼓/贝斯居中，和声铺底/装饰分左右，营造宽度；
  * 动态对比——段落间乐器进出要有设计（如 verse 去掉踩镲、chorus 加 crash 推高潮）；
  * 节奏律动——鼓组 backbeat（军鼓在 2/4 拍）要稳，副歌可加 fill 转折；
- rationale 说明编曲层次与各段落乐器进出设计。""",
    Stage.MIX: """阶段：混音（mix）—— 必须覆盖前两阶段产生的所有轨道，达到出版级混音标准。
【工程一致性】本阶段在同一个 Logic Pro 工程上操作，严禁输出 project 字段，严禁 open/close。
为所有已有轨道设置音量、声相、均衡、压缩、发送等，并用 automation 与 plugin_params 做动态混音。
要求：
- 增益分级（Gain Staging）：每条轨道 gain_stage_db 设 -18~-12dBFS，headroom_db 设 -6dB，留足余量给母带；
- 频率避让（Frequency Masking）：每条轨道设 frequency_slot 标明频段占用，冲突处用 EQ 侧链避让；
  * 底鼓 30-80Hz、贝斯 60-250Hz 必须用 sidechain_from 让贝斯在底鼓 hit 时避让；
  * 主旋律 200Hz-5kHz 切除 200Hz 以下避免与贝斯打架，副旋律让出 1-3kHz 给主旋律；
- 电平平衡：主旋律/人声 -3dB 居中；贝斯 -6dB 居中；鼓组分轨设声相（底鼓军鼓居中、踩镲左右）；
- 总线结构：用 buses 建 Reverb Bus（Space Designer）+ Drum Bus（编组鼓组）+ Vocal Bus（编组人声）；
  鼓组/人声分别经 bus_target 编组后再送主输出，便于整体控制；
- 插件链：鼓组 Channel EQ(切低频残留)+Compressor(Threshold-20/Ratio4)；人声 DeEsser+Channel EQ+Compressor；
- 用 plugin_params 精调每个插件参数（不只挂插件，要给出具体 Threshold/Ratio/Gain/Freq 值）；
- 立体声宽度：低频轨道 stereo_width=0(单声道)，高频铺底 stereo_width=1.5(加宽)；
- automation 写动态：副歌提升主旋律 -3dB、尾奏渐弱到 -12dB、副歌踩镲声相微动；
- 用 transports 定位到关键段落检查（如 goto 副歌小节）；
- 输出 mix 数组覆盖全部已有轨道（主旋律、和弦、鼓组、贝斯、铺底等），不得遗漏；
- rationale 说明混音思路：频率布局、动态设计、空间感。""",
    Stage.MASTER: """阶段：母带（master）—— 整首作品的最后一步，达到商业发行响度标准。
【工程一致性】本阶段在同一个 Logic Pro 工程上操作，严禁输出 project 字段，严禁 open/close。
导出（bounce）在当前工程上进行，导出文件落到系统配置的 render_dir。
在主输出施加母带链并设置导出参数，同时输出 master_spec 明确响度目标。
要求：
- 母带链顺序：Channel EQ（修整低频/去浑浊）→ Multiband Compressor（胶合）→
  Stereo Width（加宽但低频保持单声道）→ Limiter（响度）→ 可选 Dither；
- 必须输出 master_spec 明确目标：
  * target_lufs: 流媒体 -14.0（Spotify/Apple Music默认）；club 电子 -8；CD -9~-6
  * true_peak_ceiling: -1.0（流媒体）；-0.3（CD）
  * lra_target: 流行 6.0、电子 5.0、古典 14.0（不要过度压缩）
  * platform: streaming | cd | club | broadcast | film
  * 多段增益：低频 -1~-2dB（控制浑浊）、中频 0~+1dB、高频 +1~+2dB（提亮）
- 用 plugin_params 精调母带链（Limiter Ceiling=true_peak_ceiling、Attack 5ms、Release 80ms）；
- 用 automation 让母带链在尾奏轻微提亮（EQ 高频自动化 +1dB）；
- bounce 导出 wav/24bit/44100Hz（或 48kHz 影视）；交付混音师可设 stems=true；
- 可用 actions 在导出前 save 保存工程（系统会在流水线结束时自动保存，此处可选）；
- rationale 里总结整首作品的制作思路、最终响度/动态目标、与商业发行的匹配度。""",
}


# ---------- 视觉驱动规划（基于 Logic Pro 截图） ----------
VISUAL_SYSTEM = """你是世界级音乐制作人，正在通过截图观察用户屏幕上的 Logic Pro 窗口，像人类一样一步步精确操作它。

你将收到：当前 Logic Pro 窗口的截图 + 用户的目标 + 历史操作记录。
你必须只输出一个合法 JSON 对象（VisualStep），不要输出任何解释文字或 markdown 代码块。
JSON 必须可被 python json.loads 直接解析，严格符合以下 schema：
{
  "observation": "对当前截图的观察（中文）：看到了什么、Logic Pro 当前状态（播放/停止/编辑哪个区）、轨道/片段/混音器/钢琴卷帘可见性等",
  "plan": "这一步打算做什么（中文，简短）",
  "done": false,
  "transports": [{"op":"goto","bar":1}],
  "tracks": [],
  "region_ops": [],
  "mix": [],
  "buses": [],
  "plugin_params": [],
  "automation": [],
  "markers": [],
  "tempo_changes": [],
  "master_plugins": [],
  "record": null,
  "bounce": null,
  "actions": [],
  "rationale": "为什么这么做（中文）"
}

动作字段全部复用 StageResult 的 schema（可按需输出子集，不需要的字段可省略或留空数组）。
关键原则：
- 像人类看着屏幕操作一样：先观察截图里 Logic Pro 的实际状态，再决定下一步；
- 每次只输出「一步」可执行的操作（少量动作），不要一次规划整首作品；
- 用 actions 切换视图（open_piano_roll/open_mixer/zoom_fit）让你能看清要操作的区域；
- 用 transports 定位到要编辑的位置（goto/set_cycle）再操作片段；
- 当你在截图里看到目标已达成（如导出完成、混音台已设好、片段已量化），把 done 设为 true；
- 若截图与目标无关或 Logic Pro 未显示预期内容，用 actions 切换视图或 transports 定位；
- observation 必须基于截图实际内容，不要臆测看不到的东西。
"""

VISUAL_SCHEMA = """
返回的 JSON 结构（动作字段按需输出子集，与 StageResult 一致）：
{
  "observation": "截图观察（中文）",
  "plan": "这一步计划（中文）",
  "done": false,
  "transports": [{"op":"play|stop|goto|set_cycle","bar":1,"start_bar":1,"end_bar":32}],
  "tracks": [{"name":"主旋律","type":"software","instrument":"Piano","color":0}],
  "track_stacks": [{"name":"鼓组","members":["底鼓"],"stack_type":"folder"}],
  "region_ops": [{"op":"quantize|copy|move|transpose|split|loop","track":"鼓组","grid":"1/16","to_bar":9,"semitones":12}],
  "mix": [{"track":"主旋律","volume_db":-6,"pan":0,"mute":false,"solo":false,"plugins":[],"sends":[]}],
  "buses": [{"name":"Reverb Bus","input":"Bus 1","plugins":[{"name":"Space Designer","preset":"Large Hall"}]}],
  "plugin_params": [{"track":"鼓组","plugin":"Compressor","parameter":"Threshold","value":-20}],
  "automation": [{"track":"主旋律","parameter":"Volume","mode":"latch","points":[{"bar":1,"value":-6,"shape":"linear"}]}],
  "markers": [{"name":"Chorus","bar":9,"length_bars":8}],
  "tempo_changes": [{"bar":17,"bpm":104,"ramp":true}],
  "master_plugins": [{"name":"Limiter","preset":"Loud"}],
  "record": {"track":"人声","armed":true,"count_in":1,"autopunch":{"start_bar":9,"end_bar":16}},
  "bounce": {"format":"wav","bit_depth":24,"sample_rate":44100,"filename":"final","stems":false},
  "actions": [{"op":"save|open_mixer|open_piano_roll|zoom_fit|undo"}],
  "rationale": "为什么这么做（中文）"
}
"""


class WebAIDriver:
    """通过 Playwright 驱动用户已登录的网页 AI。"""

    def __init__(self, cfg: dict):
        ai = cfg.get("ai", {})
        self.provider = ai.get("provider", "doubao")
        self.url = ai.get("web_url") or DEFAULT_URLS.get(self.provider, "")
        # 选择器：provider 画像 + 用户覆盖
        profile = PROVIDER_PROFILES.get(self.provider, DEFAULT_SELECTORS)
        self.selectors = {**profile, **(ai.get("selectors") or {})}
        self.timeout = float(ai.get("timeout", 180))
        self.new_chat_per_stage = bool(ai.get("new_chat_per_stage", False))  # 默认 False:保持上下文连续,不每阶段开新对话
        self.retries = int(ai.get("retries", 2))

        b = cfg.get("browser", {})
        self.mode = b.get("mode", "cdp")          # cdp | persistent
        self.cdp_url = b.get("cdp_url", "http://127.0.0.1:9222")
        self.user_data_dir = b.get("user_data_dir", "~/.ai-daw-conductor/browser-profile")
        self.headless = bool(b.get("headless", False))
        self.screenshot_dir = b.get("screenshot_dir", "~/.ai-daw-conductor/screenshots")

        self._pw = None
        self._browser = None
        self._context = None
        self._page = None

    @property
    def available(self) -> bool:
        """Playwright 已安装且配置了目标 URL。"""
        try:
            import playwright  # noqa: F401
        except Exception:
            return False
        return bool(self.url)

    @property
    def connected(self) -> bool:
        return self._context is not None

    async def health_check(self) -> bool:
        """检查浏览器/页面是否仍可响应。用于长任务中检测断连。"""
        if not self._context or not self._page:
            return False
        try:
            await self._page.evaluate("1+1")
            return True
        except Exception as e:
            log.warning("网页 AI 健康检查失败：%s", e)
            return False

    async def reconnect(self, log_cb=None) -> bool:
        """关闭旧连接并重新建立。返回是否成功。"""
        if log_cb:
            await log_cb("warn", "网页 AI 连接异常，正在重连...")
        try:
            await self.close()
        except Exception:
            pass
        try:
            await self._connect()
            await self.ensure_page()
            if log_cb:
                await log_cb("info", "网页 AI 已重连。")
            return True
        except Exception as e:
            log.error("重连失败：%s", e)
            if log_cb:
                await log_cb("error", f"重连失败：{e}")
            return False

    # ---------- 连接 ----------
    async def _connect(self):
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()

        if self.mode == "cdp":
            log.info("通过 CDP 连接 Chrome: %s", self.cdp_url)
            self._browser = await self._pw.chromium.connect_over_cdp(self.cdp_url)
            ctxs = self._browser.contexts
            self._context = ctxs[0] if ctxs else await self._browser.new_context()
        else:
            from pathlib import Path
            ud = Path(self.user_data_dir).expanduser()
            ud.mkdir(parents=True, exist_ok=True)
            log.info("启动持久化 Chromium（用户目录 %s）", ud)
            self._context = await self._pw.chromium.launch_persistent_context(
                str(ud), headless=self.headless, args=["--start-maximized"]
            )

    async def ensure_page(self):
        if not self._context:
            await self._connect()
        # 复用已打开的目标网页 AI 标签页
        for p in self._context.pages:
            if self.url and self.url_host in (p.url or ""):
                self._page = p
                await p.bring_to_front()
                return p
        # 否则新建标签页打开
        self._page = await self._context.new_page()
        await self._page.goto(self.url, wait_until="domcontentloaded", timeout=60000)
        await self._page.wait_for_timeout(2000)
        return self._page

    @property
    def url_host(self) -> str:
        m = re.search(r"https?://([^/]+)", self.url or "")
        return m.group(1) if m else ""

    # ---------- 发送与读取 ----------
    async def chat(self, prompt: str, log_cb=None, new_chat: bool = False) -> str:
        """向网页 AI 发送一条 prompt，返回最新一条助手回复文本。"""
        page = await self.ensure_page()
        if log_cb:
            await log_cb("info", f"向网页 AI 注入提示（{len(prompt)} 字）...")

        if new_chat:
            await self._start_new_chat(page, log_cb)
            await page.wait_for_timeout(800)

        before_count = await self._assistant_msg_count(page)

        await self._type_and_send(page, prompt)
        await self._wait_for_completion(page, before_count, log_cb)

        text = await self._last_assistant_text(page)
        if log_cb:
            await log_cb("info", f"网页 AI 回复已抓取（{len(text)} 字）")
        return text

    async def chat_with_image(self, prompt: str, image_path: str,
                              log_cb=None, new_chat: bool = False) -> str:
        """向网页 AI 发送「图片 + 文本」多模态消息，返回助手回复文本。

        实现策略：先找到页面里的文件上传 input（input[type=file]）并 set_input_files
        注入截图，等几秒让图片预览/识别完成，再输入 prompt 文本并发送。
        各家网页 AI 的上传入口不同，这里用多策略兜底：
          1. 直接找 input[type=file]（最通用）
          2. 点击「上传图片/附件」按钮触发文件选择，再 set_input_files
        若上传失败则退化为纯文本 chat（提示 AI 缺少视觉输入）。
        """
        page = await self.ensure_page()
        if log_cb:
            await log_cb("info", f"向网页 AI 注入截图+提示（图片 {image_path}，提示 {len(prompt)} 字）...")

        if new_chat:
            await self._start_new_chat(page, log_cb)
            await page.wait_for_timeout(800)

        uploaded = await self._upload_image(page, image_path, log_cb)
        if not uploaded and log_cb:
            await log_cb("warn", "截图上传失败，退化为纯文本模式（AI 将缺少视觉输入）。")

        before_count = await self._assistant_msg_count(page)
        await self._type_and_send(page, prompt)
        await self._wait_for_completion(page, before_count, log_cb)

        text = await self._last_assistant_text(page)
        if log_cb:
            await log_cb("info", f"网页 AI 回复已抓取（{len(text)} 字）")
        return text

    async def listen_to_audio(self, audio_path: str, focus: Optional[str] = None,
                              log_cb=None) -> Optional[dict]:
        """网页 AI 听取音频（多模态：把音频文件上传给支持音频的网页 AI）。

        把音频文件作为多模态输入喂给网页 AI（豆包/Kimi/Claude 等支持音频输入），
        AI 返回听觉反馈：听到了什么、有什么问题、改进建议。

        Args:
            audio_path: 音频文件路径
            focus: 试听关注点（如"主旋律亮度"）
            log_cb: 日志回调
        Returns:
            AudioFeedback 的 dict 形式，失败返回 None
        """
        if not self._pw or not self._page:
            return None
        try:
            from pathlib import Path
            path = Path(audio_path).expanduser()
            if not path.exists():
                log.warning("音频文件不存在：%s", audio_path)
                return None

            page = await self.ensure_page()
            if log_cb:
                await log_cb("info", f"向网页 AI 注入音频+提示（音频 {audio_path}，关注点 {focus or '整体听感'}）...")

            # 网页 AI 的文件上传：找 input[type=file] 并 set_input_files
            # 多数网页 AI 支持上传音频文件（拖拽或点击附件按钮）
            uploaded = False
            try:
                file_input = page.locator("input[type='file']").first
                if await file_input.count():
                    await file_input.set_input_files(str(path))
                    # 等待音频预览/识别完成
                    await page.wait_for_timeout(1500)
                    uploaded = True
                    if log_cb:
                        await log_cb("info", "音频已上传到网页 AI。")
            except Exception as e:
                log.debug("input[type=file] 上传音频失败：%s", e)

            # 评价请求 prompt
            if uploaded:
                prompt = (
                    f"请听这段音频并评价。关注点：{focus or '整体听感'}。"
                    "请回答：1)听到了什么（音色/动态/空间感）2)有什么问题 3)改进建议 4)评分 1-10。"
                    '只输出 JSON：{"heard":"...","issues":["..."],"suggestions":["..."],"rating":6}'
                )
            else:
                # 兜底：用文本描述让 AI 评价（降级模式，没有真正听到）
                if log_cb:
                    await log_cb("warn", "音频上传失败，退化为纯文本模式（AI 将无法真正听到音频）。")
                prompt = (
                    f"我刚导出了一段音频（指定小节范围），但因技术原因无法上传给你听。"
                    f"请基于音乐制作经验说说如何评估{focus or '整体听感'}，"
                    "并指出常见问题与改进建议。"
                )

            before_count = await self._assistant_msg_count(page)
            await self._type_and_send(page, prompt)
            await self._wait_for_completion(page, before_count, log_cb)
            text = await self._last_assistant_text(page)
            if not text:
                return None
            if log_cb:
                await log_cb("info", f"网页 AI 听觉反馈已抓取（{len(text)} 字）")

            # 从回复中提取 JSON
            match = re.search(r'\{[^{}]*"heard"[^{}]*\}', text, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group())
                except Exception:
                    pass
            # 提取失败时把全文当 heard 返回
            return {
                "heard": text[:500],
                "issues": [],
                "suggestions": [],
                "rating": 6,
            }
        except Exception as e:
            log.warning("网页 AI 听取音频失败：%s", e)
            return None

    async def _upload_image(self, page, image_path: str, log_cb=None) -> bool:
        """把图片上传到网页 AI 输入框。返回是否成功。"""
        from pathlib import Path
        p = Path(image_path).expanduser()
        if not p.exists():
            log.warning("截图文件不存在：%s", image_path)
            return False

        # 策略 1：直接找 input[type=file]
        try:
            file_input = page.locator("input[type='file']").first
            if await file_input.count():
                await file_input.set_input_files(str(p))
                # 等待图片预览/识别
                await page.wait_for_timeout(2500)
                if log_cb:
                    await log_cb("info", "截图已上传到网页 AI。")
                return True
        except Exception as e:
            log.debug("input[type=file] 上传失败：%s", e)

        # 策略 2：点击「上传图片/附件」按钮触发文件选择框，再监听 filechooser
        upload_btn_selectors = [
            "button:has-text('上传')", "button:has-text('图片')",
            "button:has-text('附件')", "[class*='upload']", "[class*='attach']",
            "[class*='image-btn']", "button[aria-label*='上传']", "button[aria-label*='图片']",
        ]
        for sel in upload_btn_selectors:
            try:
                btn = page.locator(sel).first
                if await btn.count():
                    async with page.expect_file_chooser(timeout=5000) as fc_info:
                        await btn.click()
                    file_chooser = await fc_info.value
                    await file_chooser.set_files(str(p))
                    await page.wait_for_timeout(2500)
                    if log_cb:
                        await log_cb("info", f"截图已通过 {sel} 上传到网页 AI。")
                    return True
            except Exception:
                continue

        log.warning("未找到网页 AI 的图片上传入口。")
        return False

    async def _start_new_chat(self, page, log_cb=None):
        """点击「新对话」按钮，避免上一阶段的上下文污染本阶段决策。"""
        sel = self.selectors.get("new_chat") or ""
        if not sel:
            return
        try:
            btn = page.locator(sel).first
            if await btn.count():
                await btn.click(timeout=5000)
                if log_cb:
                    await log_cb("info", "已开启新对话（避免上下文污染）。")
        except Exception:
            # 静默失败：新对话非必需
            pass

    async def screenshot(self, tag: str = "debug") -> Optional[str]:
        """失败时截图，便于排查网页 AI 改版/未登录等问题。返回路径或 None。"""
        if not self._page:
            return None
        try:
            from pathlib import Path
            from datetime import datetime
            d = Path(self.screenshot_dir).expanduser()
            d.mkdir(parents=True, exist_ok=True)
            name = f"{datetime.now():%Y%m%d_%H%M%S}_{tag}.png"
            path = d / name
            await self._page.screenshot(path=str(path), full_page=False)
            log.info("截图已保存：%s", path)
            return str(path)
        except Exception as e:
            log.debug("截图失败：%s", e)
            return None

    async def _type_and_send(self, page, prompt: str):
        # 聚焦输入框
        sel = self.selectors["input"] or DEFAULT_SELECTORS["input"]
        try:
            await page.locator(sel).first.wait_for(state="visible", timeout=15000)
        except Exception:
            # 兜底：找页面里任意 textarea / contenteditable
            await page.locator("textarea, [contenteditable='true']").first.wait_for(state="visible", timeout=15000)
            sel = "textarea, [contenteditable='true']"

        loc = page.locator(sel).first
        await loc.click(timeout=10000)
        await page.wait_for_timeout(150)

        # 判断输入类型
        itype = self.selectors["input_type"]
        if itype == "auto":
            tag = await loc.evaluate("el => el.tagName.toLowerCase()")
            itype = "textarea" if tag == "textarea" else "contenteditable"

        if itype == "contenteditable":
            await loc.evaluate("(el, txt) => { el.focus(); document.execCommand('insertText', false, txt); }", prompt)
        else:
            await loc.fill(prompt, timeout=10000)

        await page.wait_for_timeout(200)

        # 发送
        sent = False
        send_sel = self.selectors.get("send") or ""
        if send_sel:
            try:
                btn = page.locator(send_sel).last
                if await btn.count() and await btn.is_enabled():
                    await btn.click()
                    sent = True
            except Exception:
                sent = False
        if not sent and self.selectors.get("send_via_enter", True):
            await page.keyboard.press("Enter")
            sent = True
        if not sent:
            # 最后兜底：按回车
            await page.keyboard.press("Enter")
        await page.wait_for_timeout(500)

    async def _assistant_msg_count(self, page) -> int:
        return await page.evaluate("""() => {
            const els = document.querySelectorAll(
                "[class*='message'], [class*='answer'], [class*='receive'], [class*='assistant'], [data-role='assistant']"
            );
            return els.length;
        }""")

    async def _wait_for_completion(self, page, before_count, log_cb=None):
        """等待流式回复完成：助手段落数增加 + 文本稳定 / 停止按钮消失。"""
        timeout = self.timeout
        deadline = asyncio.get_event_loop().time() + timeout
        stable_for = 0.0
        last_text = ""
        last_change = asyncio.get_event_loop().time()
        min_wait = asyncio.get_event_loop().time() + 3.0  # 至少等 3s 避免误判

        if log_cb:
            await log_cb("info", "等待网页 AI 生成完成...")

        while True:
            now = asyncio.get_event_loop().time()
            if now > deadline:
                if log_cb:
                    await log_cb("warn", f"等待超时（{timeout}s），按当前文本继续。")
                return

            count = await self._assistant_msg_count(page)
            text = await self._last_assistant_text(page)
            generating = await self._is_generating(page)

            if text != last_text:
                last_text = text
                last_change = now
                stable_for = 0.0
            else:
                stable_for = now - last_change

            # 完成条件：已有新助手段落 且 文本稳定 1.2s 且 不在生成中 且 超过最小等待
            if (count > before_count and stable_for >= 1.2
                    and not generating and now > min_wait):
                return

            await asyncio.sleep(0.5)

    async def _is_generating(self, page) -> bool:
        sel = self.selectors.get("generating") or ""
        if sel:
            try:
                if await page.locator(sel).count():
                    return True
            except Exception:
                pass
        # 兜底：检测「停止生成」类按钮
        return bool(await page.evaluate("""() => {
            const btns = [...document.querySelectorAll('button, [role="button"]')];
            return btns.some(b => /停止|stop|生成中|generating/i.test(b.getAttribute('aria-label') + ' ' + b.className + ' ' + (b.innerText||'')));
        }"""))

    async def _last_assistant_text(self, page) -> str:
        sel = self.selectors.get("response") or ""
        if sel:
            try:
                cnt = await page.locator(sel).count()
                if cnt:
                    return await page.locator(sel).nth(cnt - 1).inner_text(timeout=5000)
            except Exception:
                pass
        # JS 兜底：取最后一条助手消息容器文本
        return await page.evaluate("""() => {
            const sels = "[class*='message'], [class*='answer'], [class*='receive'], [class*='assistant'], [data-role='assistant']";
            let els = [...document.querySelectorAll(sels)];
            if (!els.length) {
                // 退而求其次：抓所有长文本块
                els = [...document.querySelectorAll('article, section, div')].filter(e => e.innerText && e.innerText.length > 80);
            }
            if (!els.length) return '';
            return els[els.length - 1].innerText.trim();
        }""")

    async def close(self):
        """关闭 Playwright 会话。加 5 秒超时防止 _pw.stop() 在浏览器无响应时永久挂起。"""
        try:
            if self._pw:
                await asyncio.wait_for(self._pw.stop(), timeout=5.0)
        except asyncio.TimeoutError:
            log.warning("Playwright stop 超时（5s），强制丢弃引用。")
        except Exception:
            pass
        self._pw = self._browser = self._context = self._page = None


class AIEngine:
    """对外接口保持不变（generate_stage / generate_full / online）。"""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.driver = WebAIDriver(cfg)
        self._use_web = self.driver.available
        if not self._use_web:
            log.info("Playwright 未安装或未配置网页 AI，AI 引擎将运行在 demo（内置生成器）模式。"
                     "安装 playwright 后可驱动网页版豆包/Kimi/通义千问。")

    @property
    def online(self) -> bool:
        return self._use_web

    async def generate_stage(
        self,
        stage: Stage,
        user_prompt: str,
        context: Optional[str] = None,
        log_cb=None,
    ) -> StageResult:
        """生成单阶段决策：优先走网页 AI，失败/离线降级到 demo 生成器。"""
        if self._use_web:
            try:
                return await self._generate_via_web(stage, user_prompt, context, log_cb)
            except Exception as e:
                log.warning("网页 AI 调用失败 (%s)，降级到内置生成器。", e)
                if log_cb:
                    await log_cb("warn", f"网页 AI 调用失败：{e}，本阶段降级为 demo。")
        return self._generate_demo(stage, user_prompt, context)

    async def health_check(self) -> bool:
        """转发到 driver 的健康检查（demo 模式恒为 True）。"""
        if not self._use_web:
            return True
        return await self.driver.health_check()

    async def reconnect(self, log_cb=None) -> bool:
        if not self._use_web:
            return True
        return await self.driver.reconnect(log_cb=log_cb)

    async def listen_to_audio(self, audio_path: str, focus: Optional[str] = None,
                              log_cb=None) -> Optional[dict]:
        """让 AI 听取音频文件并给出听觉反馈。

        把音频文件作为多模态输入喂给网页 AI（豆包/Kimi/Claude 等支持音频输入），
        AI 返回听觉反馈：听到了什么、有什么问题、改进建议。

        Args:
            audio_path: 音频文件路径
            focus: 试听关注点（如"主旋律亮度"）
            log_cb: 日志回调
        Returns:
            AudioFeedback 的 dict 形式，失败返回 None
        """
        # 默认实现：用文本描述让 AI 评价（降级模式，没有真正听）
        # 子类 WebAIDriver 会覆盖此方法，真正把音频上传给多模态 AI
        if not self._use_web:
            # demo 模式无网页 AI，无法真正听音频
            return None
        return await self.driver.listen_to_audio(audio_path, focus=focus, log_cb=log_cb)

    async def generate_free_action(self, goal: str, context: str, step: int) -> Optional[FreeAction]:
        """AI 主动决策下一步即兴动作。

        与 generate_stage 的区别:
        - 无 stage 概念,AI 自由决定做什么
        - 上下文包含操作历史 + 听觉反馈
        - AI 可以 satisfied=True 表示完成

        demo 模式(无网页 AI)返回 None,由调用方决定如何处理。
        """
        if not self._use_web:
            # demo 模式:无网页 AI,无法做即兴决策
            return None
        prompt = f"""{IMPROVISE_PROMPT}

{context}

当前是第 {step} 步。请决定下一步做什么。
如果觉得作品已经完成,设 satisfied=true。

用户目标:{goal}
"""
        try:
            raw = await self.driver.chat(prompt, new_chat=False)
            data = self._extract_json(raw)
            if data is None:
                log.warning("generate_free_action: 未能从回复中解析 JSON(前80字:%r)",
                            raw[:80] if raw else "")
                return None
            return FreeAction.model_validate(data)
        except Exception as e:
            log.warning("generate_free_action 失败: %s", e)
            return None

    async def evaluate_stage(
        self,
        stage: Stage,
        result: "StageResult",
        context: Optional[str] = None,
        log_cb=None,
    ) -> tuple[bool, str]:
        """让 AI 自评估本阶段产出是否达标。返回 (是否可接受, 反馈意见)。

        demo 模式下直接通过（无网页 AI 可问）。
        """
        if not self._use_web:
            return True, ""
        prompt = (
            "你是音乐制作质检员。请评估下面这一阶段的产出是否符合专业标准。\n"
            f"阶段：{stage.value}\n"
            f"产出摘要：{result.summary}\n"
            f"创作思路：{result.rationale or '(无)'}\n"
            f"轨道数：{len(result.tracks)}，MIDI 片段数：{len(result.regions)}，"
            f"混音条目数：{len(result.mix)}，母带插件数：{len(result.master_plugins)}\n"
            + (f"已有上下文：\n{context}\n" if context else "")
            + "\n请只输出 JSON：{\"acceptable\": true/false, \"issues\": [\"问题1\", ...], \"feedback\": \"改进建议（中文，无则空字符串）\"}"
        )
        try:
            raw = await self.driver.chat(prompt, log_cb=log_cb, new_chat=False)
            data = self._extract_json(raw)
            if data and "acceptable" in data:
                acceptable = bool(data["acceptable"])
                issues = data.get("issues", [])
                feedback = data.get("feedback", "")
                if issues:
                    feedback = ("问题：" + "；".join(issues) + "。") + feedback
                if log_cb:
                    tag = "通过" if acceptable else "需改进"
                    await log_cb("info", f"自评估[{stage.value}]：{tag}" + (f" — {feedback}" if feedback else ""))
                return acceptable, feedback
            return True, ""
        except Exception as e:
            log.warning("自评估失败（%s），按通过处理。", e)
            if log_cb:
                await log_cb("warn", f"自评估调用失败：{e}，按通过处理。")
            return True, ""

    async def generate_full(self, user_prompt: str, context: Optional[str] = None,
                            log_cb=None) -> "AsyncIterator[StageResult]":
        accumulated = context or ""
        for stage in [Stage.COMPOSE, Stage.ARRANGE, Stage.MIX, Stage.MASTER]:
            result = await self.generate_stage(stage, user_prompt, accumulated, log_cb)
            accumulated += f"\n[{stage.value}] {result.summary}"
            yield result

    # ---------- 网页 AI 路径 ----------
    async def _generate_via_web(self, stage: Stage, user_prompt: str,
                                context: Optional[str], log_cb) -> StageResult:
        full = (
            SYSTEM_BASE + "\n" + SCHEMA_DESC + "\n" + STAGE_PROMPTS[stage]
            + (f"\n已有上下文（前一阶段产出，需在此基础上延续）：\n{context}" if context else "")
            + f"\n\n用户需求：{user_prompt or '请按你的专业判断完成本阶段。'}"
            + '\n\n请只输出上述 schema 的 JSON 对象，不要包裹在 markdown 代码块里。'
        )
        # 每阶段开新对话，避免上一阶段的上下文干扰本阶段决策
        is_first = stage == Stage.COMPOSE
        new_chat = self.driver.new_chat_per_stage and not is_first

        last_err = None
        for attempt in range(1, self.driver.retries + 2):  # 初次 + retries 次重试
            try:
                if log_cb and attempt > 1:
                    await log_cb("info", f"第 {attempt} 次尝试调用网页 AI...")
                raw = await self.driver.chat(full, log_cb=log_cb, new_chat=new_chat)
                data = self._extract_json(raw)
                if data is None:
                    raise ValueError(f"未能从回复中解析 JSON（前80字：{raw[:80]!r}）")
                data = dict(data)
                data["stage"] = stage.value
                result = StageResult.model_validate(data)
                # 把 AI 原始回复附在 rationale 后，供前端预览
                if raw and not result.rationale:
                    result.rationale = raw[:300]
                return result
            except Exception as e:
                last_err = e
                log.warning("网页 AI 第 %d 次失败：%s", attempt, e)
                if log_cb:
                    await log_cb("warn", f"第 {attempt} 次失败：{e}")
                # 失败时截图便于排查
                await self.driver.screenshot(tag=f"{stage.value}_fail{attempt}")
                if attempt <= self.driver.retries:
                    backoff = 2 * attempt
                    if log_cb:
                        await log_cb("info", f"{backoff}s 后重试...")
                    await asyncio.sleep(backoff)
        raise last_err  # type: ignore[misc]

    @staticmethod
    def _extract_json(raw: str) -> Optional[dict]:
        """从网页 AI 回复中鲁棒地提取 JSON。

        处理：思考标签 <think>…</think>、markdown 代码块、前后解释文字、
        多余尾随内容、单引号、尾随逗号等常见问题。
        """
        if not raw:
            return None
        text = raw.strip()

        # 1. 去除思考过程（部分模型会输出 <think>…</think>）
        text = re.sub(r"<think(?:\s[^>]*)?>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<reasoning(?:\s[^>]*)?>.*?</reasoning>", "", text, flags=re.DOTALL | re.IGNORECASE)

        # 2. 优先提取代码块里的 json
        code_blocks = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
        for block in reversed(code_blocks):
            parsed = AIEngine._try_parse(block)
            if parsed is not None:
                return parsed

        # 3. 截取最外层 { ... }
        s, e = text.find("{"), text.rfind("}")
        if s != -1 and e != -1 and e > s:
            candidate = text[s:e + 1]
            parsed = AIEngine._try_parse(candidate)
            if parsed is not None:
                return parsed

        # 4. 最后兜底：直接整段
        return AIEngine._try_parse(text)

    @staticmethod
    def _try_parse(s: str) -> Optional[dict]:
        s = s.strip()
        # 去除尾随逗号（JSON 不允许，但 LLM 常加）
        s = re.sub(r",\s*([}\]])", r"\1", s)
        try:
            obj = json.loads(s)
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None

    # ---------- Demo 生成器（离线可用） ----------
    def _generate_demo(self, stage: Stage, user_prompt: str, context: Optional[str]) -> StageResult:
        if stage == Stage.COMPOSE:
            return self._demo_compose(user_prompt)
        if stage == Stage.ARRANGE:
            return self._demo_arrange(user_prompt, context)
        if stage == Stage.MIX:
            return self._demo_mix(user_prompt, context)
        return self._demo_master(user_prompt, context)

    def _demo_compose(self, prompt: str) -> StageResult:
        key = "A minor"
        root = "A3"
        bpm = 100
        structure = ["intro", "verse", "chorus", "verse", "chorus", "outro"]
        section_len = 16
        melody_notes: list[NoteSpec] = []
        chord_notes: list[NoteSpec] = []
        scale = mt.scale_notes(root, "minor", 2)
        chord_roots = ["A2", "F2", "G2", "A2"]
        for si, sec in enumerate(structure):
            base = si * section_len
            for b in range(section_len):
                idx = (si * 4 + b) % len(scale)
                melody_notes.append(NoteSpec(
                    pitch=scale[idx], start=float(base + b), duration=1.0, velocity=85 + (b % 3) * 5
                ))
            cr = chord_roots[si % len(chord_roots)]
            for iv in [0, 3, 7]:
                chord_notes.append(NoteSpec(
                    pitch=mt.resolve_pitch(cr) + iv,
                    start=float(base), duration=float(section_len), velocity=70
                ))
        return StageResult(
            stage=Stage.COMPOSE,
            summary=f"Demo 作曲：{key}，{bpm}BPM，{len(structure)}段，AI 生成主旋律与和弦。",
            project=ProjectPlan(
                title="AI 习作 No.1", genre="Pop/Ballad",
                tempo=TempoSpec(bpm=bpm, time_signature="4/4", key=key),
                structure=structure, key=key,
                description="由内置生成器创作的 A 小调示范曲，含主旋律与和弦进行。",
            ),
            tracks=[
                TrackSpec(name="主旋律", type="software", instrument="Acoustic Grand Piano"),
                TrackSpec(name="和弦", type="software", instrument="Acoustic Grand Piano"),
            ],
            regions=[
                MidiRegionSpec(track="主旋律", start=0.0, instrument="Acoustic Grand Piano", notes=melody_notes),
                MidiRegionSpec(track="和弦", start=0.0, instrument="Acoustic Grand Piano", notes=chord_notes),
            ],
            rationale="以 A 小调为基底，主旋律走音阶动机，和弦用 Am-F-G 循环营造流行抒情氛围。",
        )

    def _demo_arrange(self, prompt: str, context: Optional[str]) -> StageResult:
        section_len = 16
        structure = ["intro", "verse", "chorus", "verse", "chorus", "outro"]
        drums: list[NoteSpec] = []
        bass: list[NoteSpec] = []
        pad: list[NoteSpec] = []
        bass_roots = ["A1", "F1", "G1", "A1"]
        for si, sec in enumerate(structure):
            base = si * section_len
            if sec in ("verse", "chorus", "outro"):
                for b in range(section_len):
                    if b % 4 == 0:
                        drums.append(NoteSpec(pitch=36, start=float(base + b), duration=0.5, velocity=110))
                    if b % 4 == 2:
                        drums.append(NoteSpec(pitch=38, start=float(base + b), duration=0.5, velocity=100))
                    for h in range(2):
                        drums.append(NoteSpec(pitch=42, start=float(base + b + h * 0.5), duration=0.25, velocity=70))
            if sec in ("chorus", "outro"):
                for b in range(0, section_len, 2):
                    drums.append(NoteSpec(pitch=49, start=float(base + b), duration=0.25, velocity=90))
            br = bass_roots[si % len(bass_roots)]
            for b in range(0, section_len, 2):
                bass.append(NoteSpec(pitch=br, start=float(base + b), duration=2.0, velocity=95))
            pad_root = mt.resolve_pitch(bass_roots[si % len(bass_roots)].replace("1", "3"))
            for iv in [0, 7, 12]:
                pad.append(NoteSpec(pitch=pad_root + iv, start=float(base), duration=float(section_len), velocity=55))
        return StageResult(
            stage=Stage.ARRANGE,
            summary="Demo 编曲：新增鼓组、贝斯、和声铺底三轨，对齐各段落。",
            tracks=[
                TrackSpec(name="鼓组", type="software", instrument="Drum Kit Designer"),
                TrackSpec(name="贝斯", type="software", instrument="Synth Bass"),
                TrackSpec(name="和声铺底", type="software", instrument="Synth Pad"),
            ],
            regions=[
                MidiRegionSpec(track="鼓组", instrument="Drums", notes=drums),
                MidiRegionSpec(track="贝斯", instrument="Synth Bass", notes=bass),
                MidiRegionSpec(track="和声铺底", instrument="Synth Pad", notes=pad),
            ],
            rationale="verse/chorus/outro 加入鼓组与贝斯节奏支撑，chorus 加 crash 推高潮，pad 铺底统一空间感。",
        )

    def _demo_mix(self, prompt: str, context: Optional[str]) -> StageResult:
        tracks = ["主旋律", "和弦", "鼓组", "贝斯", "和声铺底"]
        mix: list[MixParams] = []
        for t in tracks:
            vol = -6
            pan = 0.0
            plugins: list[PluginSpec] = []
            sends: list[SendSpec] = []
            if t == "主旋律":
                vol, pan = -3, 0.0
                plugins = [PluginSpec(name="Channel EQ", preset="Vocal"), PluginSpec(name="Compressor")]
                sends = [SendSpec(target="Reverb Bus", amount=0.35)]
            elif t == "鼓组":
                vol, pan = -6, 0.0
                plugins = [PluginSpec(name="Drum Machine Designer"), PluginSpec(name="Compressor")]
            elif t == "贝斯":
                vol, pan = -5, 0.0
                plugins = [PluginSpec(name="Channel EQ")]
            elif t == "和弦":
                vol, pan = -10, -0.2
                sends = [SendSpec(target="Reverb Bus", amount=0.25)]
            elif t == "和声铺底":
                vol, pan = -12, 0.3
                sends = [SendSpec(target="Reverb Bus", amount=0.5)]
            mix.append(MixParams(track=t, volume_db=vol, pan=pan, mute=False, solo=False,
                                 plugins=plugins or None, sends=sends or None))
        mix.append(MixParams(track="Reverb Bus", volume_db=0.0, pan=0.0,
                             plugins=[PluginSpec(name="Space Designer", preset="Large Hall")]))
        return StageResult(
            stage=Stage.MIX,
            summary="Demo 混音：分配音量声相，主旋律/人声加 EQ+压缩+混响发送，建立 Reverb Bus。",
            tracks=[TrackSpec(name="Reverb Bus", type="audio", instrument=None)],
            mix=mix,
            rationale="主旋律居中突出，鼓组与贝斯构成能量骨架，铺底声相分散营造宽度，统一经 Reverb Bus 串起空间。",
        )

    def _demo_master(self, prompt: str, context: Optional[str]) -> StageResult:
        return StageResult(
            stage=Stage.MASTER,
            summary="Demo 母带：主输出 EQ->压缩->限制器链，目标 -14 LUFS。",
            master_plugins=[
                PluginSpec(name="Channel EQ", preset="Mastering EQ"),
                PluginSpec(name="Compressor", preset="Glue"),
                PluginSpec(name="Limiter", preset="Loud -1dBTP"),
            ],
            bounce=BounceSpec(format="wav", bit_depth=24, sample_rate=44100,
                              normalize=False, filename="ai_daw_conductor_master"),
            rationale="母带链先修整低频淤积，再用胶合压缩提升密度，最后限制器推响度至流媒体标准。",
        )

    async def plan_from_screenshot(
        self,
        goal: str,
        image_path: str,
        history: str = "",
        log_cb=None,
    ) -> VisualStep:
        """让网页 AI 看着 Logic Pro 截图，输出下一步操作（VisualStep）。

        Args:
            goal: 用户的最终目标（如「把副歌主旋律复制到第9小节」）
            image_path: Logic Pro 窗口截图路径
            history: 之前已执行的操作记录（让 AI 知道已经做了什么，避免重复）
        Returns:
            VisualStep：AI 的观察、计划、动作清单、是否完成
        """
        prompt = (
            VISUAL_SYSTEM + "\n" + VISUAL_SCHEMA + "\n"
            + f"用户目标：{goal}\n"
            + (f"已执行操作记录：\n{history}\n" if history else "（尚无历史操作）\n")
            + "请基于截图观察 Logic Pro 当前状态，输出下一步操作。"
            + "若目标已达成，done 设为 true 并在 observation 说明依据。"
            + "\n\n请只输出上述 schema 的 JSON 对象，不要包裹在 markdown 代码块里。"
        )
        if not self._use_web:
            # demo 模式：无法真正看图，返回一个空步并标记完成避免死循环
            if log_cb:
                await log_cb("warn", "demo 模式无法做视觉规划，直接返回完成。")
            return VisualStep(
                observation="demo 模式：未连接网页 AI，无法识别截图。",
                plan="跳过视觉规划",
                done=True,
                rationale="demo 模式无视觉能力，直接结束视觉循环。",
            )
        try:
            raw = await self.driver.chat_with_image(prompt, image_path, log_cb=log_cb, new_chat=False)
            data = self._extract_json(raw)
            if data is None:
                raise ValueError(f"未能从视觉回复中解析 JSON（前80字：{raw[:80]!r}）")
            step = VisualStep.model_validate(data)
            if raw and not step.rationale:
                step.rationale = raw[:200]
            return step
        except Exception as e:
            log.warning("视觉规划失败：%s", e)
            if log_cb:
                await log_cb("error", f"视觉规划失败：{e}")
            # 失败时返回一个 done 步，避免视觉循环死循环
            return VisualStep(
                observation=f"视觉规划失败：{e}",
                plan="终止视觉循环",
                done=True,
                rationale="网页 AI 调用失败，终止视觉循环以防死循环。",
            )

    async def close(self):
        await self.driver.close()
