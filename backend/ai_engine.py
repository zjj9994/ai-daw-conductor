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
    BounceSpec, MixParams, MidiRegionSpec, NoteSpec, PluginSpec,
    ProjectPlan, SendSpec, Stage, StageResult, TempoSpec, TrackSpec,
)
from . import music_theory as mt

log = logging.getLogger("ai_engine")


# ---------- 各网页 AI 默认入口 ----------
DEFAULT_URLS = {
    "doubao": "https://www.doubao.com/chat/",
    "kimi": "https://kimi.moonshot.cn/",
    "qwen": "https://tongyi.aliyun.com/qianwen/",
    "zhipu": "https://chatglm.cn/main/detail/",
    "custom": "",
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


SYSTEM_BASE = """你是世界级音乐制作人，精通 Logic Pro 工作流，将自主、不间断地完成一整首作品。
你必须只输出一个合法 JSON 对象，不要输出任何解释文字、markdown 代码块或注释。
JSON 必须可被 python json.loads 直接解析，并严格符合给定的 schema。
所有文本字段（summary、rationale、name、title、genre、description、instrument 等）使用简体中文。
音高 pitch 可以是 MIDI 数字（如 60）或音名（如 'C4'）；时间 start/duration 单位为「拍(beat)」。

关键原则：
- 整首作品必须连贯：段落之间有逻辑递进（如前奏→主歌→副歌→桥段→尾奏），调性统一，速度一致。
- 后续阶段必须延续前一阶段已确定的标题、调性、速度、段落结构与轨道命名，不得推翻重来。
- 你的目标是产出完整的、可独立播放的成品，而非片段 demo。
"""

SCHEMA_DESC = """
返回的 JSON 结构（字段可按阶段省略不需要的）：
{
  "stage": "compose|arrange|mix|master",
  "summary": "本阶段决策简述（中文）",
  "project": {"title":"作品名","genre":"风格","tempo":{"bpm":100,"time_signature":"4/4","key":"C minor"},"structure":["intro","verse","chorus","verse","chorus","bridge","chorus","outro"],"key":"C minor","description":"作品创意描述"},
  "tracks": [{"name":"主旋律","type":"software","instrument":"Acoustic Grand Piano"}],
  "regions": [{"track":"主旋律","start":0,"instrument":"Acoustic Grand Piano","notes":[{"pitch":"C4","start":0,"duration":1,"velocity":90}]}],
  "mix": [{"track":"主旋律","volume_db":-6,"pan":0,"mute":false,"solo":false,"plugins":[{"name":"Channel EQ","preset":"Vocal"}],"sends":[{"target":"Reverb Bus","amount":0.3}]}],
  "master_plugins": [{"name":"Limiter","preset":"Loud"}],
  "bounce": {"format":"wav","bit_depth":24,"sample_rate":44100,"normalize":false,"filename":"final_master"},
  "rationale": "创作思路解释（中文）"
}
"""

STAGE_PROMPTS = {
    Stage.COMPOSE: """阶段：作曲（compose）—— 这是整首作品的根基，后续所有阶段都基于你现在的决策。
请完成：确定作品标题、风格、速度、调性、段落结构，并生成主旋律与和声的 MIDI 音符。
要求：
- 主旋律要有记忆点、符合调性、节奏自然；至少给出主旋律、和弦/和声两轨；
- 段落结构至少 4 段（建议 intro-verse-chorus-verse-chorus-bridge-chorus-outro 等完整流行曲式）；
- 为每个段落在相应轨道上给出音符（start 相对全曲的拍数）；总长度 16-32 小节；
- 在 project.structure 里明确列出段落顺序与每段的小节数（可写在 description 里）；
- rationale 里说明动机发展、段落对比与情感走向，让后续阶段能延续你的创意。""",
    Stage.ARRANGE: """阶段：编曲（arrange）—— 必须延续作曲阶段确定的标题/调性/速度/段落结构。
在已有作曲基础上完成：决定乐器配置与各声部 MIDI。
要求：
- 不得修改已有的主旋律与和弦轨道内容，只能新增轨道；
- 新增鼓组、贝斯、和声铺底、装饰/副旋律等轨道并编写 MIDI 片段；
- 鼓组用 MIDI 数字：底鼓 36、军鼓 38、踩镲 42、开镲 46、嗵鼓 50/48、镲 49；
- 贝斯走根音与节奏支撑，和声铺底用长音 pad；各 region 的 start 要对齐段落；
- 根据段落属性安排密度：intro 稀疏、verse 适中、chorus 饱满、bridge 做对比。""",
    Stage.MIX: """阶段：混音（mix）—— 必须覆盖前两阶段产生的所有轨道。
为所有已有轨道设置音量、声相、均衡、压缩、发送等。
要求：
- 主旋律/人声靠中、-3dB 左右；贝斯居中 -6dB；鼓组分轨设声相；
- 给鼓组加 Channel EQ + Compressor，给人声加 DeEsser + Reverb 发送；
- 建立一条 Reverb Bus 辅助通道并让需要空间的轨道发送；
- 输出 mix 数组覆盖全部已有轨道（主旋律、和弦、鼓组、贝斯、铺底等），不得遗漏。""",
    Stage.MASTER: """阶段：母带（master）—— 整首作品的最后一步，输出最终成品。
在主输出施加母带链并设置导出参数。
要求：
- 顺序 Channel EQ（修整低频）-> Compressor（胶合）-> Limiter（响度）；
- Limiter 目标 -1 dBTP，响度约 -14 LUFS；
- 给出 bounce 导出参数（wav/24bit/44100Hz）；
- rationale 里总结整首作品的制作思路与最终听感目标。""",
}


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
        self.new_chat_per_stage = bool(ai.get("new_chat_per_stage", True))
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
        try:
            if self._pw:
                await self._pw.stop()
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

    async def health_check(self) -> bool:
        """转发到 driver 的健康检查（demo 模式恒为 True）。"""
        if not self._use_web:
            return True
        return await self.driver.health_check()

    async def reconnect(self, log_cb=None) -> bool:
        if not self._use_web:
            return True
        return await self.driver.reconnect(log_cb=log_cb)

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

    async def close(self):
        await self.driver.close()
