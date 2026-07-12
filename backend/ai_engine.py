"""AI 引擎：调用网页端 AI 模型（豆包 / 火山方舟 Ark，OpenAI 兼容接口）生成制作决策。

设计要点：
  - 使用 OpenAI SDK 指向 Volcengine Ark 的 base_url，兼容豆包全系模型。
  - 通过 system prompt 约束 AI 输出严格 JSON，再用 pydantic 校验为 StageResult。
  - 若未配置有效 api_key，自动降级到内置生成器（demo 模式），保证项目可独立运行与演示。
  - 每个阶段（compose/arrange/mix/master）有专属 prompt。
"""
from __future__ import annotations

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


SYSTEM_BASE = """你是世界级音乐制作人，精通 Logic Pro 工作流。
你必须只输出一个合法 JSON 对象，不要输出任何解释文字、markdown 代码块或注释。
JSON 必须可被 python json.loads 直接解析，并严格符合给定的 schema。
所有文本字段（summary、rationale、name、title、genre、description、instrument 等）使用简体中文。
音高 pitch 可以是 MIDI 数字（如 60）或音名（如 'C4'）；时间 start/duration 单位为「拍(beat)」。
"""

SCHEMA_DESC = """
返回的 JSON 结构（字段可按阶段省略不需要的）：
{
  "stage": "compose|arrange|mix|master",
  "summary": "本阶段决策简述（中文）",
  "project": {                          // 仅 compose 必填
    "title": "作品名",
    "genre": "风格",
    "tempo": {"bpm": 100, "time_signature": "4/4", "key": "C minor"},
    "structure": ["intro","verse","chorus","verse","chorus","bridge","chorus","outro"],
    "key": "C minor",
    "description": "作品创意描述"
  },
  "tracks": [{"name":"主旋律","type":"software","instrument":"Acoustic Grand Piano"}],
  "regions": [                          // MIDI 片段，作曲/编曲阶段
    {
      "track":"主旋律","start":0,
      "instrument":"Acoustic Grand Piano",
      "notes":[{"pitch":"C4","start":0,"duration":1,"velocity":90}]
    }
  ],
  "mix": [                              // 混音参数，mix 阶段
    {"track":"主旋律","volume_db":-6,"pan":0,"mute":false,"solo":false,
     "plugins":[{"name":"Channel EQ","preset":"Vocal"}],"sends":[{"target":"Reverb Bus","amount":0.3}]}
  ],
  "master_plugins": [                   // 母带插件，master 阶段
    {"name":"Limiter","preset":"Loud"}
  ],
  "bounce": {"format":"wav","bit_depth":24,"sample_rate":44100,"normalize":false,"filename":"final_master"},
  "rationale": "创作思路解释（中文）"
}
"""

STAGE_PROMPTS = {
    Stage.COMPOSE: """阶段：作曲（compose）
请完成：确定作品标题、风格、速度、调性、段落结构，并生成主旋律与和声的 MIDI 音符。
要求：
- 主旋律要有记忆点、符合调性、节奏自然。
- 至少给出 主旋律、和弦/和声 两轨。
- 段落结构至少 4 段；为每个段落在相应轨道上给出音符（start 相对全曲的拍数）。
- 总长度建议 16-32 小节（4/4 下即 64-128 拍）。
""",
    Stage.ARRANGE: """阶段：编曲（arrange）
在已有作曲基础上完成：决定乐器配置与各声部 MIDI。
要求：
- 新增 鼓组、贝斯、和声铺底、装饰/副旋律 等轨道，并为每个轨道编写 MIDI 片段。
- 鼓组用 MIDI 数字：底鼓 36、军鼓 38、踩镲 42、开镲 46、嗵鼓 50/48、镲 49。
- 贝斯走根音与节奏支撑，和声铺底用长音 pad。
- 各 region 的 start 要对齐段落。
""",
    Stage.MIX: """阶段：混音（mix）
为所有已有轨道设置音量、声相、均衡、压缩、发送等。
要求：
- 主旋律/人声靠中、-3dB 左右；贝斯居中 -6dB；鼓组分轨设声相。
- 给鼓组加 Channel EQ + Compressor，给人声加 DeEsser + Reverb 发送。
- 建立一条 Reverb Bus 辅助通道并让需要空间的轨道发送。
- 输出 mix 数组覆盖全部轨道。
""",
    Stage.MASTER: """阶段：母带（master）
在主输出施加母带链并设置导出参数。
要求：
- 顺序：Channel EQ（修整低频）-> Compressor（胶合）-> Limiter（响度）。
- Limiter 目标 -1 dBTP，响度约 -14 LUFS（流媒体友好）。
- 给出 bounce 导出参数。
""",
}


class AIEngine:
    def __init__(self, cfg: dict):
        self.cfg = cfg.get("ai", {})
        self.api_key = self.cfg.get("api_key", "")
        self.base_url = self.cfg.get("base_url", "https://ark.cn-beijing.volces.com/api/v3")
        self.model = self.cfg.get("model", "")
        self.temperature = float(self.cfg.get("temperature", 0.8))
        self.max_tokens = int(self.cfg.get("max_tokens", 4096))
        self.timeout = float(self.cfg.get("timeout", 120))
        self._client = None
        self._init_client()

    def _init_client(self):
        if self.api_key and self.api_key.startswith("YOUR_") is False and self.model:
            try:
                from openai import AsyncOpenAI
                self._client = AsyncOpenAI(
                    api_key=self.api_key,
                    base_url=self.base_url,
                    timeout=self.timeout,
                )
            except Exception as e:  # pragma: no cover
                log.warning("OpenAI 客户端初始化失败，使用内置生成器: %s", e)
                self._client = None
        else:
            log.info("未配置有效 ai.api_key/model，AI 引擎将运行在 demo（内置生成器）模式。")

    @property
    def online(self) -> bool:
        return self._client is not None

    async def generate_stage(
        self,
        stage: Stage,
        user_prompt: str,
        context: Optional[str] = None,
    ) -> StageResult:
        """生成单个阶段的制作决策。"""
        if self.online:
            try:
                return await self._generate_via_llm(stage, user_prompt, context)
            except Exception as e:
                log.warning("LLM 调用失败 (%s)，降级到内置生成器。", e)
        return self._generate_demo(stage, user_prompt, context)

    async def generate_full(self, user_prompt: str, context: Optional[str] = None) -> AsyncIterator[StageResult]:
        """完整流水线：依次产出 compose/arrange/mix/master 四个阶段。"""
        accumulated = context or ""
        for stage in [Stage.COMPOSE, Stage.ARRANGE, Stage.MIX, Stage.MASTER]:
            result = await self.generate_stage(stage, user_prompt, accumulated)
            accumulated += f"\n[{stage.value}] {result.summary}"
            yield result

    # ---------- LLM 路径 ----------
    async def _generate_via_llm(self, stage: Stage, user_prompt: str, context: Optional[str]) -> StageResult:
        sys = SYSTEM_BASE + "\n" + SCHEMA_DESC + "\n" + STAGE_PROMPTS[stage]
        if context:
            sys += f"\n已有上下文（前一阶段产出，需在此基础上延续）：\n{context}"

        resp = await self._client.chat.completions.create(
            model=self.model,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": sys},
                {"role": "user", "content": user_prompt or "请按你的专业判断完成本阶段。"},
            ],
        )
        raw = resp.choices[0].message.content or "{}"
        data = self._extract_json(raw)
        return self._validate(stage, data, user_prompt)

    @staticmethod
    def _extract_json(raw: str) -> dict:
        raw = raw.strip()
        # 去掉可能的 ```json ... ``` 包裹
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
        # 截取第一个 { 到最后一个 }
        s, e = raw.find("{"), raw.rfind("}")
        if s != -1 and e != -1 and e > s:
            raw = raw[s:e + 1]
        return json.loads(raw)

    def _validate(self, stage: Stage, data: dict, user_prompt: str) -> StageResult:
        data = dict(data)
        data["stage"] = stage.value
        # 规范化 pitch：交给 pydantic 校验，music_theory 在执行时再解析
        return StageResult.model_validate(data)

    # ---------- Demo 生成器（离线可用） ----------
    def _generate_demo(self, stage: Stage, user_prompt: str, context: Optional[str]) -> StageResult:
        """无 API key 时的内置生成器，产出完整可执行的 StageResult。"""
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
        section_len = 16  # 拍
        melody_notes: list[NoteSpec] = []
        chord_notes: list[NoteSpec] = []
        # A 小调音阶上行旋律 + 和弦
        scale = mt.scale_notes(root, "minor", 2)
        progressions = [[0, 5, 3, 4], [0, 5, 3, 7]]  # Am F G Am 风格根音
        chord_roots = ["A2", "F2", "G2", "A2"]
        for si, sec in enumerate(structure):
            base = si * section_len
            for b in range(section_len):
                # 旋律：每拍一个音
                idx = (si * 4 + b) % len(scale)
                melody_notes.append(NoteSpec(
                    pitch=scale[idx], start=float(base + b), duration=1.0, velocity=85 + (b % 3) * 5
                ))
            # 和弦：每 4 拍一个长和弦
            cr = chord_roots[si % len(chord_roots)]
            for ci, iv in enumerate([0, 3, 7]):  # 三和弦
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
                    # 底鼓 1、3 拍；军鼓 2、4 拍；踩镲每半拍
                    if b % 4 == 0:
                        drums.append(NoteSpec(pitch=36, start=float(base + b), duration=0.5, velocity=110))
                    if b % 4 == 2:
                        drums.append(NoteSpec(pitch=38, start=float(base + b), duration=0.5, velocity=100))
                    for h in range(2):
                        drums.append(NoteSpec(pitch=42, start=float(base + b + h * 0.5), duration=0.25, velocity=70))
            if sec in ("chorus", "outro"):
                for b in range(0, section_len, 2):
                    drums.append(NoteSpec(pitch=49, start=float(base + b), duration=0.25, velocity=90))  # crash
            # 贝斯：每 2 拍一个根音
            br = bass_roots[si % len(bass_roots)]
            for b in range(0, section_len, 2):
                bass.append(NoteSpec(pitch=br, start=float(base + b), duration=2.0, velocity=95))
            # 和声铺底
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
