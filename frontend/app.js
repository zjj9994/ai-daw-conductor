/* AI-DAW-Conductor 前端逻辑：WebSocket 事件流 + UI 更新 + 背景示波器 */
"use strict";

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

// ---------- 状态 ----------
const state = {
  mode: "pipeline",       // pipeline | stage | autonomous | visual
  stage: "compose",
  running: false,
  tracks: [],
  logCount: 0,
  pollTimer: null,        // 任务状态轮询计时器
  lastPrompt: "",         // 最近一次创作指令（用于重试）
  selfEval: true,         // 自主模式是否启用自评估
  visualStepCount: 0,     // 视觉模式已执行步数
  selectedProvider: "doubao",  // 当前选中的网页 AI（顶栏快捷切换 + 设置弹层共用）
};

const STAGE_NAMES = { compose: "作曲", arrange: "编曲", mix: "混音", master: "母带" };
const MODE_LABELS = {
  pipeline: "全流程",
  stage: "单阶段",
  autonomous: "自主制作",
  visual: "视觉操作",
};

// ---------- WebSocket ----------
let ws = null;
let reconnectTimer = null;

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => { $("#chip-ai") && null; };
  ws.onmessage = (ev) => {
    try { handle(JSON.parse(ev.data)); } catch (e) { console.warn(e); }
  };
  ws.onclose = () => {
    if (!reconnectTimer) reconnectTimer = setTimeout(connect, 2000);
  };
  ws.onerror = () => { try { ws.close(); } catch (e) {} };
}

function send(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
}

// ---------- 事件处理 ----------
function handle(evt) {
  if (evt.type !== "daw_event") return;
  const k = evt.kind;
  switch (k) {
    case "connected":
      setChip("#chip-ai", evt.ai_online, evt.ai_online ? "网页 AI 就绪" : "demo 模式");
      break;
    case "log":
      addLog(evt.level || "info", evt.message, "log");
      break;
    case "project_created":
      renderProject(evt);
      break;
    case "track_added":
      addTrack(evt.track);
      break;
    case "region_added":
      addLog("info", `片段已写入「${evt.track}」(${evt.note_count} 音符)`, "event");
      break;
    case "midi_generated":
      addLog("info", `MIDI 生成：${evt.note_count} 音符 → ${evt.path}`, "event");
      break;
    case "mix_applied":
      addMix(evt.track, evt.params);
      break;
    case "master_applied":
      addLog("info", `母带链：${evt.plugins.map((p) => p.name).join(" → ")}`, "event");
      break;
    case "transport":
      addLog("info", transportLabel(evt), "event");
      break;
    case "region_op":
      addLog("info", regionOpLabel(evt), "event");
      break;
    case "marker_added":
      addLog("info", `标记：${evt.name} @ 小节 ${evt.bar}`, "event");
      break;
    case "tempo_change":
      addLog("info", `速度变化：${evt.bpm}BPM @ 小节 ${evt.bar}${evt.ramp ? "（渐变）" : ""}`, "event");
      break;
    case "bus_created":
      addLog("info", `总线「${evt.name}」已创建（${evt.plugins.length} 个插件）`, "event");
      break;
    case "plugin_param":
      addLog("info", `插件参数：${evt.track}/${evt.plugin}/${evt.parameter} = ${evt.value}`, "event");
      break;
    case "automation":
      addLog("info", `自动化：${evt.track}/${evt.parameter}（${evt.point_count} 点，${evt.mode}）`, "event");
      break;
    case "record_setup":
      addLog("info", `录音准备：${evt.track}（armed=${evt.armed}）`, "event");
      break;
    case "track_stack_created":
      addLog("info", `轨道堆栈「${evt.name}」：${evt.members.join("、")}`, "event");
      break;
    case "ui_action":
      addLog("info", `UI：${evt.op}`, "event");
      break;
    case "project_saved":
      addLog("info", "工程已保存", "event");
      break;
    case "visual_start":
      addLog("info", `视觉规划启动：目标「${evt.goal}」（最多 ${evt.max_steps} 步）`, "event");
      state.visualStepCount = 0;
      $("#visual-step-count").textContent = "0";
      $("#visual-steps").innerHTML = '<div class="empty">等待 AI 看图规划...</div>';
      break;
    case "screenshot_captured":
      updateVisualScreenshot(evt.path);
      if (evt.step && evt.step > 0) {
        addLog("info", `截图完成（步骤 ${evt.step}）`, "event");
      }
      break;
    case "visual_step":
      addVisualStep(evt);
      break;
    case "visual_done":
      if (evt.reason === "no_ai") {
        addLog("warn", "视觉模式需先在设置里连接网页 AI。", "event");
      } else if (evt.reason === "no_screenshot") {
        addLog("warn", "截图失败（非 macOS 或 Logic Pro 未运行），视觉循环终止。", "event");
      } else if (evt.reason === "max_steps") {
        addLog("warn", `视觉循环达最大步数 ${evt.steps} 停止（AI 未判定完成）。`, "event");
      } else {
        addLog("info", `✓ 视觉规划完成（共 ${evt.steps} 步）`, "event");
      }
      break;
    case "bounce_done":
      renderBounce(evt.path);
      // 不直接 add（无索引无法下载），改为拉取服务端历史以保证下载链接可用
      loadRenderHistory();
      break;
    case "stage_start":
      setStageState(evt.stage, "running");
      addLog("info", `▶ ${evt.summary}`, "event");
      break;
    case "stage_done":
      setStageState(evt.stage, "done");
      if (evt.rationale) addLog("info", `创作思路：${evt.rationale}`, "event");
      break;
    case "ai_result":
      addLog("info", `[AI·${evt.stage}] ${evt.summary}`, "event");
      renderAIPreview(evt.stage, evt.summary, evt.rationale);
      break;
    case "pipeline_progress":
      setStageState(evt.completed, "done");
      break;
    case "pipeline_done":
      addLog("info", `流水线完成：${evt.stages.join(" → ")}`, "event");
      setRunning(false);
      refreshTaskStatus();
      break;
    case "task_finished":
      setRunning(false);
      refreshTaskStatus();
      break;
    case "error":
      addLog("error", evt.message, "log");
      setRunning(false);
      refreshTaskStatus();
      break;
  }
}

// ---------- UI 渲染 ----------
function setChip(sel, on, label) {
  const el = $(sel);
  if (!el) return;
  el.classList.toggle("on", !!on);
  const dot = el.querySelector(".dot");
  if (dot) dot.style.background = on ? "var(--accent)" : "var(--warn)";
  el.lastChild.textContent = " " + label;
}

function setRunning(on) {
  state.running = on;
  $("#btn-run").disabled = on;
  $("#btn-cancel").disabled = !on;
  $("#btn-run").textContent = on ? "制作中…" : "开始制作";
  if (on) startPolling(); else stopPolling();
}

function startPolling() {
  stopPolling();
  state.pollTimer = setInterval(refreshTaskStatus, 1500);
}

function stopPolling() {
  if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
}

function addLog(level, message, cls) {
  const log = $("#log");
  const row = document.createElement("div");
  row.className = "row kind-" + (cls || "log");
  const ts = new Date().toLocaleTimeString("zh-CN", { hour12: false });
  row.innerHTML = `<span class="ts">${ts}</span><span class="lvl ${level}">${level.toUpperCase()}</span><span class="msg"></span>`;
  row.querySelector(".msg").textContent = message;
  log.appendChild(row);
  log.scrollTop = log.scrollHeight;
  state.logCount += 1;
  $("#log-count").textContent = state.logCount;
}

function setStageState(stage, st) {
  const node = $(`#pipeline .stage-node[data-stage="${stage}"]`);
  if (!node) return;
  node.classList.remove("running", "done", "error");
  const stateEl = node.querySelector(".node-state");
  if (st === "running") {
    node.classList.add("running");
    stateEl.textContent = "执行中";
  } else if (st === "done") {
    node.classList.add("done");
    stateEl.textContent = "完成";
  } else if (st === "error") {
    node.classList.add("error");
    stateEl.textContent = "失败";
  } else {
    stateEl.textContent = "待命";
  }
}

function resetPipeline() {
  $$("#pipeline .stage-node").forEach((n) => setStageState(n.dataset.stage, "idle"));
}

function renderProject(evt) {
  $("#project-info").innerHTML = `
    <h3>${escapeHtml(evt.title || "未命名")}</h3>
    <dl class="meta">
      <dt>BPM</dt><dd>${evt.bpm}</dd>
      <dt>调性</dt><dd>${escapeHtml(evt.key || "-")}</dd>
      <dt>拍号</dt><dd>${escapeHtml(evt.time_signature || "4/4")}</dd>
    </dl>`;
}

function addTrack(track) {
  state.tracks.push(track);
  const box = $("#tracks");
  if (state.tracks.length === 1) box.innerHTML = "";
  const card = document.createElement("div");
  card.className = "track-card";
  card.innerHTML = `<span class="tdot"></span><span class="tname"></span><span class="tinst"></span>`;
  card.querySelector(".tname").textContent = track.name;
  card.querySelector(".tinst").textContent = track.instrument || track.type;
  box.appendChild(card);
  $("#track-count").textContent = state.tracks.length;
}

function addMix(track, params) {
  const box = $("#mixer");
  if (box.querySelector(".empty")) box.innerHTML = "";
  const vol = params.volume_db;
  const existing = box.querySelector(`[data-mix="${cssEscape(track)}"]`);
  if (existing) existing.remove();
  const row = document.createElement("div");
  row.className = "mix-row";
  row.dataset.mix = track;
  const pct = vol != null ? Math.max(0, Math.min(100, ((vol + 60) / 66) * 100)) : 50;
  const pan = params.pan != null ? `Δ${params.pan > 0 ? "R" : "L"}${Math.abs(params.pan).toFixed(2)}` : "C";
  row.innerHTML = `<span class="mname"></span><div class="mbar"><div class="mfill" style="width:${pct}%"></div></div><span class="mval">${vol != null ? vol.toFixed(1) + "dB" : pan}</span>`;
  row.querySelector(".mname").textContent = track;
  if (params.plugins && params.plugins.length) {
    const pl = document.createElement("div");
    pl.className = "mix-plugins";
    pl.style.gridColumn = "1 / -1";
    pl.innerHTML = params.plugins.map((p) => `<span>${escapeHtml(p.name)}</span>`).join("");
    row.appendChild(pl);
  }
  box.appendChild(row);
}

function renderBounce(path) {
  $("#render").innerHTML = `
    <div class="render-box">
      <div class="render-icon">◈</div>
      <div class="render-info">
        <b>母带已导出</b><br/>
        <span class="path">${escapeHtml(path)}</span>
      </div>
    </div>`;
  addLog("info", `导出完成：${path}`, "event");
}

function renderAIPreview(stage, summary, rationale) {
  const stageNames = { compose: "作曲", arrange: "编曲", mix: "混音", master: "母带" };
  const box = $("#ai-preview");
  if (box.querySelector(".empty")) box.innerHTML = "";
  const card = document.createElement("div");
  card.style.marginBottom = "12px";
  card.innerHTML = `<span class="stage-tag">${escapeHtml(stageNames[stage] || stage)}</span>
    <div class="summary"></div>
    <div class="rationale"></div>`;
  card.querySelector(".summary").textContent = summary || "";
  card.querySelector(".rationale").textContent = rationale ? "AI 思路：" + rationale : "";
  box.appendChild(card);
  box.scrollTop = box.scrollHeight;
}

function transportLabel(evt) {
  const map = {
    play: "▶ 播放", stop: "■ 停止", pause: "⏸ 暂停", record: "● 录音",
    goto: `定位 → 小节 ${evt.bar || 1}`, rewind: "◀ 倒退", forward: "▶ 前进",
    toggle_loop: "切换循环",
    set_cycle: `循环区：小节 ${evt.start_bar}-${evt.end_bar}`,
    set_loop: `循环区：小节 ${evt.start_bar}-${evt.end_bar}`,
  };
  return "传输：" + (map[evt.op] || evt.op);
}

function regionOpLabel(evt) {
  const map = {
    split: "切割", join: "合并", move: `移动 → 小节 ${evt.to_bar || 1}`,
    copy: `复制 → 小节 ${evt.to_bar || 1}`, delete: "删除", loop: "循环",
    resize: "调整长度", quantize: "量化", transpose: "移调", crop: "裁剪",
  };
  return `片段操作：「${evt.track || "-"}」${map[evt.op] || evt.op}`;
}

// ---------- 视觉模式 ----------
function updateVisualScreenshot(path) {
  if (!path) return;
  const img = $("#visual-shot-img");
  const empty = $("#visual-shot-empty");
  if (!img) return;
  // 用服务端接口读取截图文件，加时间戳防缓存
  img.src = `/api/screenshot/file?path=${encodeURIComponent(path)}&_t=${Date.now()}`;
  img.style.display = "block";
  if (empty) empty.style.display = "none";
}

function addVisualStep(evt) {
  const box = $("#visual-steps");
  if (!box) return;
  if (box.querySelector(".empty")) box.innerHTML = "";
  state.visualStepCount = (state.visualStepCount || 0) + 1;
  $("#visual-step-count").textContent = state.visualStepCount;

  const card = document.createElement("div");
  card.className = "visual-step-card" + (evt.done ? " done" : "");
  const tag = evt.done ? "✓ 完成" : `步骤 ${evt.step}`;
  card.innerHTML = `<div class="vs-head"><span class="vs-tag">${tag}</span></div>
    <div class="vs-obs"></div>
    <div class="vs-plan"></div>`;
  card.querySelector(".vs-obs").textContent = "观察：" + (evt.observation || "");
  card.querySelector(".vs-plan").textContent = "计划：" + (evt.plan || "");
  if (evt.rationale) {
    const r = document.createElement("div");
    r.className = "vs-rationale";
    r.textContent = "理由：" + evt.rationale;
    card.appendChild(r);
  }
  box.appendChild(card);
  box.scrollTop = box.scrollHeight;
}

function addRenderHistory(filename, path, idx) {
  const box = $("#render-history");
  if (box.querySelector(".empty")) box.innerHTML = "";
  const item = document.createElement("div");
  item.className = "render-item";
  const time = new Date().toLocaleTimeString("zh-CN", { hour12: false });
  item.innerHTML = `<span class="ricon">◈</span><span class="rname"></span><span class="rmeta"></span><a class="rlink" title="下载">↓</a>`;
  item.querySelector(".rname").textContent = filename;
  item.querySelector(".rmeta").textContent = time;
  item.title = path;
  // 下载链接：用索引（服务端按索引查找）
  const link = item.querySelector(".rlink");
  if (idx != null) {
    link.href = `/api/renders/${idx}/download`;
    link.target = "_blank";
  } else {
    link.style.display = "none";
  }
  box.insertBefore(item, box.firstChild);
  const count = box.querySelectorAll(".render-item").length;
  $("#render-count").textContent = count;
}

async function loadRenderHistory() {
  try {
    const r = await (await fetch("/api/renders")).json();
    const box = $("#render-history");
    box.innerHTML = "";
    if (!r.count) {
      box.innerHTML = '<div class="empty">尚未导出任何作品</div>';
      $("#render-count").textContent = "0";
      return;
    }
    // 服务端 renders 按时间顺序；前端倒序展示，索引对应原列表
    r.renders.forEach((rec, i) => {
      addRenderHistory(rec.filename, rec.path, i);
    });
    $("#render-count").textContent = r.count;
  } catch (e) {}
}

async function clearRenderHistory() {
  if (!confirm("确定清空渲染历史记录吗？（不会删除磁盘文件）")) return;
  try {
    await fetch("/api/renders", { method: "DELETE" });
    await loadRenderHistory();
    addLog("info", "已清空渲染历史", "event");
  } catch (e) {
    addLog("error", "清空失败：" + e, "log");
  }
}

function setProgress(pct, label) {
  $("#progress-fill").style.width = Math.max(0, Math.min(100, pct * 100)) + "%";
  $("#progress-label").textContent = label || "";
}

async function refreshTaskStatus() {
  try {
    const s = await (await fetch("/api/task/status")).json();
    setProgress(s.progress, s.state === "running"
      ? `${STAGE_NAMES[s.current_stage] || s.current_stage || ""} ${Math.round(s.progress * 100)}%`
      : (s.state === "done" ? "已完成" : s.state === "error" ? "出错" : s.state === "cancelled" ? "已取消" : ""));
    // 同步阶段状态
    if (s.completed_stages) s.completed_stages.forEach((st) => setStageState(st, "done"));
    if (s.state === "running" && s.current_stage && !s.completed_stages.includes(s.current_stage)) {
      setStageState(s.current_stage, "running");
    }
    if (s.state === "error" && s.current_stage) setStageState(s.current_stage, "error");
    // 显示/隐藏重试按钮
    const retryBtn = $("#btn-retry");
    if (retryBtn) retryBtn.style.display = (s.state === "error") ? "" : "none";
  } catch (e) {}
}

// ---------- 诊断面板 ----------
async function openDiagnostics() {
  const modal = $("#modal-diagnostics");
  const body = $("#diag-body");
  body.innerHTML = '<div class="empty">正在运行诊断…</div>';
  modal.classList.add("open");
  try {
    const d = await (await fetch("/api/diagnostics")).json();
    renderDiagnostics(d);
  } catch (e) {
    body.innerHTML = `<div class="empty">诊断失败：${escapeHtml(String(e))}</div>`;
  }
}

function renderDiagnostics(d) {
  const body = $("#diag-body");
  const items = [
    { key: "playwright", label: "Playwright（浏览器自动化）" },
    { key: "cdp", label: "CDP 调试端口（Chrome 连接）" },
    { key: "mido", label: "MIDO（MIDI 文件生成）" },
    { key: "rtmidi", label: "python-rtmidi（实时 MIDI 输出）" },
    { key: "applescript", label: "AppleScript（Logic Pro 实控）" },
  ];
  let html = `<div class="diag-meta">
    <span>平台：<b>${escapeHtml(d.platform)}</b></span>
    <span>Python：<b>${escapeHtml(d.python)}</b></span>
    <span>AI 服务：<b>${escapeHtml(d.ai_provider)}</b></span>
    <span class="${d.ai_online ? "ok" : "err"}">${d.ai_online ? "● 网页 AI 在线" : "○ demo 模式"}</span>
  </div>`;
  html += '<div class="diag-list">';
  for (const it of items) {
    const r = d[it.key] || {};
    html += `<div class="diag-row ${r.ok ? "ok" : "err"}">
      <span class="diag-icon">${r.ok ? "✓" : "✗"}</span>
      <div class="diag-content">
        <div class="diag-label">${escapeHtml(it.label)}</div>
        <div class="diag-detail">${escapeHtml(r.detail || "-")}</div>
      </div>
    </div>`;
  }
  html += "</div>";
  if (d.suggestions && d.suggestions.length) {
    html += '<div class="diag-suggest"><b>建议：</b><ul>';
    d.suggestions.forEach((s) => { html += `<li>${escapeHtml(s)}</li>`; });
    html += "</ul></div>";
  }
  body.innerHTML = html;
}

// ---------- 阶段重试 ----------
async function retryFailedStage() {
  if (state.running) return;
  const s = await (await fetch("/api/task/status")).json();
  const failedStage = s.current_stage;
  if (!failedStage) {
    addLog("warn", "没有可重试的阶段", "log");
    return;
  }
  addLog("info", `重试阶段：${STAGE_NAMES[failedStage] || failedStage}`, "event");
  setStageState(failedStage, "idle");
  setRunning(true);
  try {
    const res = await fetch("/api/stage", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ stage: failedStage, prompt: state.lastPrompt, bpm: 100 }),
    });
    if (!res.ok) {
      const j = await res.json().catch(() => ({}));
      addLog("error", `重试失败：${j.error || res.status}`, "log");
      setRunning(false);
    }
  } catch (e) {
    addLog("error", String(e), "log");
    setRunning(false);
  }
}

// ---------- 工具 ----------
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function cssEscape(s) { try { return CSS.escape(s); } catch (e) { return s; } }

// ---------- 交互 ----------
$$(".stage-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    $$(".stage-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    if (btn.dataset.mode === "pipeline") {
      state.mode = "pipeline";
    } else if (btn.dataset.mode === "autonomous") {
      state.mode = "autonomous";
    } else if (btn.dataset.mode === "visual") {
      state.mode = "visual";
    } else {
      state.mode = "stage";
      state.stage = btn.dataset.stage;
    }
  });
});

$("#btn-run").addEventListener("click", async () => {
  if (state.running) return;
  const prompt = $("#prompt").value.trim();
  state.lastPrompt = prompt;
  resetPipeline();
  setProgress(0, "");
  const retryBtn = $("#btn-retry");
  if (retryBtn) retryBtn.style.display = "none";
  state.tracks = [];
  $("#tracks").innerHTML = '<div class="empty">无轨道</div>';
  $("#mixer").innerHTML = '<div class="empty">无混音数据</div>';
  $("#render").innerHTML = '<div class="empty">未导出</div>';
  $("#project-info").innerHTML = '<div class="empty">尚未生成作品</div>';
  $("#ai-preview").innerHTML = '<div class="empty">AI 回复将在此显示</div>';
  $("#track-count").textContent = "0";
  setRunning(true);
  addLog("info", `开始任务（${MODE_LABELS[state.mode] || state.mode}）`, "event");
  // 视觉模式重置步骤计数与面板
  state.visualStepCount = 0;
  $("#visual-step-count").textContent = "0";
  $("#visual-steps").innerHTML = '<div class="empty">等待 AI 看图规划...</div>';

  try {
    let url, body;
    if (state.mode === "visual") {
      url = "/api/visual";
      body = { goal: prompt || "检查并完善当前 Logic Pro 工程", max_steps: 20, settle_delay: 1.5 };
      addLog("info", "视觉模式：AI 将根据 Logic Pro 截图一步步精确操作。", "event");
    } else if (state.mode === "autonomous") {
      url = "/api/autonomous";
      body = { prompt, enable_self_eval: state.selfEval, max_stage_retries: 2 };
      addLog("info", "自主模式：AI 将不间断完成整首作品，含自评估与自动重连。", "event");
    } else if (state.mode === "pipeline") {
      url = "/api/pipeline";
      body = { prompt };
    } else {
      url = "/api/stage";
      body = { stage: state.stage, prompt, bpm: 100 };
    }
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const j = await res.json().catch(() => ({}));
      addLog("error", `请求失败：${j.error || res.status}`, "log");
      setRunning(false);
    }
  } catch (e) {
    addLog("error", String(e), "log");
    setRunning(false);
  }
});

$("#btn-cancel").addEventListener("click", async () => {
  try { await fetch("/api/cancel", { method: "POST" }); } catch (e) {}
  addLog("warn", "已请求取消", "log");
});

$("#btn-retry").addEventListener("click", retryFailedStage);
$("#btn-diag").addEventListener("click", openDiagnostics);
$("#btn-close-diag").addEventListener("click", () => $("#modal-diagnostics").classList.remove("open"));
$("#modal-diagnostics").addEventListener("click", (e) => {
  if (e.target === $("#modal-diagnostics")) $("#modal-diagnostics").classList.remove("open");
});

// ---------- 设置 ----------
const LS_KEY = "ai-daw-conductor-settings";
const LS_TEMPLATES = "ai-daw-conductor-templates";
const modal = $("#modal-settings");

// ---------- 创作指令模板 ----------
function loadTemplates() {
  try { return JSON.parse(localStorage.getItem(LS_TEMPLATES) || "[]"); } catch (e) { return []; }
}
function saveTemplates(arr) {
  try { localStorage.setItem(LS_TEMPLATES, JSON.stringify(arr)); } catch (e) {}
}
function renderTemplateList() {
  const box = $("#template-list");
  const tpls = loadTemplates();
  box.innerHTML = "";
  if (!tpls.length) {
    box.innerHTML = '<span class="empty">暂无模板</span>';
    return;
  }
  tpls.forEach((t, i) => {
    const chip = document.createElement("span");
    chip.className = "template-chip";
    chip.title = t.text;
    const name = document.createElement("span");
    name.textContent = t.name;
    name.addEventListener("click", () => {
      $("#prompt").value = t.text;
      $("#prompt").focus();
    });
    const del = document.createElement("span");
    del.className = "tpl-del";
    del.textContent = "×";
    del.title = "删除模板";
    del.addEventListener("click", (e) => {
      e.stopPropagation();
      const arr = loadTemplates();
      arr.splice(i, 1);
      saveTemplates(arr);
      renderTemplateList();
    });
    chip.appendChild(name);
    chip.appendChild(del);
    box.appendChild(chip);
  });
}
function saveCurrentAsTemplate() {
  const text = $("#prompt").value.trim();
  if (!text) { addLog("warn", "指令为空，无法保存为模板", "log"); return; }
  const name = text.slice(0, 12).replace(/\s+/g, " ");
  const arr = loadTemplates();
  arr.push({ name, text, ts: Date.now() });
  saveTemplates(arr);
  renderTemplateList();
  addLog("info", `已保存模板「${name}」`, "event");
}

function loadLocalSettings() {
  try { return JSON.parse(localStorage.getItem(LS_KEY) || "{}"); } catch (e) { return {}; }
}
function saveLocalSettings(obj) {
  try { localStorage.setItem(LS_KEY, JSON.stringify(obj)); } catch (e) {}
}
function collectSettingsForm() {
  return {
    provider: state.selectedProvider || "doubao",
    web_url: $("#set-weburl").value.trim(),
    timeout: parseFloat($("#set-timeout").value) || 180,
    browser_mode: $("#set-bmode").value,
    cdp_url: $("#set-cdpurl").value.trim(),
    user_data_dir: $("#set-userdata").value.trim(),
  };
}

// ---------- 网页 AI provider 目录（启动时从 /api/providers 拉取，作为兜底用静态镜像） ----------
let PROVIDER_CATALOG = null;  // 数组形式（来自 /api/providers），含 key/name/url/initial/color/vendor/region/order

const PROVIDER_CATALOG_FALLBACK = [
  { key: "doubao",   name: "豆包",       url: "https://www.doubao.com/chat/",         initial: "豆", color: "#3b82f6", vendor: "字节跳动",   region: "cn",     order: 1 },
  { key: "kimi",     name: "Kimi",       url: "https://kimi.moonshot.cn/",             initial: "K",  color: "#8b5cf6", vendor: "Moonshot",   region: "cn",     order: 2 },
  { key: "qwen",     name: "通义千问",   url: "https://tongyi.aliyun.com/qianwen/",    initial: "通", color: "#6366f1", vendor: "阿里云",     region: "cn",     order: 3 },
  { key: "zhipu",    name: "智谱清言",   url: "https://chatglm.cn/main/detail/",       initial: "智", color: "#10b981", vendor: "智谱 AI",    region: "cn",     order: 4 },
  { key: "deepseek", name: "DeepSeek",   url: "https://chat.deepseek.com/",            initial: "D",  color: "#06b6d4", vendor: "深度求索",   region: "cn",     order: 5 },
  { key: "yiyan",    name: "文心一言",   url: "https://yiyan.baidu.com/",              initial: "文", color: "#dc2626", vendor: "百度",       region: "cn",     order: 6 },
  { key: "hunyuan",  name: "腾讯混元",   url: "https://hunyuan.tencent.com/bot/chat",  initial: "混", color: "#0ea5e9", vendor: "腾讯",       region: "cn",     order: 7 },
  { key: "spark",    name: "讯飞星火",   url: "https://xinghuo.xfyun.cn/dchat",        initial: "星", color: "#f59e0b", vendor: "科大讯飞",   region: "cn",     order: 8 },
  { key: "hailuo",   name: "海螺 AI",    url: "https://hailuo.com/",                   initial: "海", color: "#fb7185", vendor: "MiniMax",    region: "cn",     order: 9 },
  { key: "chatgpt",  name: "ChatGPT",    url: "https://chat.openai.com/",              initial: "G",  color: "#10a37f", vendor: "OpenAI",     region: "global", order: 10 },
  { key: "claude",   name: "Claude",     url: "https://claude.ai/new",                 initial: "C",  color: "#d97706", vendor: "Anthropic",  region: "global", order: 11 },
  { key: "gemini",   name: "Gemini",     url: "https://gemini.google.com/",            initial: "Gm", color: "#4285f4", vendor: "Google",     region: "global", order: 12 },
  { key: "grok",     name: "Grok",       url: "https://grok.com/",                     initial: "X",  color: "#e5e7eb", vendor: "xAI",        region: "global", order: 13 },
  { key: "perplexity", name: "Perplexity", url: "https://www.perplexity.ai/",          initial: "P",  color: "#22d3ee", vendor: "Perplexity", region: "global", order: 14 },
  { key: "custom",   name: "自定义",     url: "",                                       initial: "+",  color: "#7fa39a", vendor: "任意",       region: "any",    order: 99 },
];

async function ensureProviderCatalog() {
  if (PROVIDER_CATALOG) return PROVIDER_CATALOG;
  try {
    const r = await fetch("/api/providers");
    const j = await r.json();
    if (j.ok && Array.isArray(j.providers) && j.providers.length) {
      PROVIDER_CATALOG = j.providers;
      return PROVIDER_CATALOG;
    }
  } catch (e) {}
  PROVIDER_CATALOG = PROVIDER_CATALOG_FALLBACK;
  return PROVIDER_CATALOG;
}

function findProvider(key) {
  const list = PROVIDER_CATALOG || PROVIDER_CATALOG_FALLBACK;
  return list.find((p) => p.key === key) || list.find((p) => p.key === "doubao");
}

// ---------- 设置弹层：provider 卡片网格 ----------
function renderProviderGrid(selectedKey) {
  const grid = $("#provider-grid");
  if (!grid) return;
  const list = PROVIDER_CATALOG || PROVIDER_CATALOG_FALLBACK;
  grid.innerHTML = "";
  list.forEach((p) => {
    const card = document.createElement("button");
    card.type = "button";
    card.className = "provider-card" + (p.key === selectedKey ? " selected" : "");
    card.dataset.key = p.key;
    card.title = p.vendor ? `${p.name}（${p.vendor}）` : p.name;
    card.innerHTML = `
      <span class="provider-badge" style="--pc:${p.color}">${escapeHtml(p.initial)}</span>
      <span class="provider-info">
        <span class="provider-name">${escapeHtml(p.name)}</span>
        <span class="provider-vendor">${escapeHtml(p.vendor || "")}</span>
      </span>
      <span class="provider-check" aria-hidden="true">✓</span>
    `;
    card.addEventListener("click", () => selectProviderInForm(p.key));
    grid.appendChild(card);
  });
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
}

function selectProviderInForm(key) {
  state.selectedProvider = key;
  const p = findProvider(key);
  if (!p) return;
  // 标记选中卡片
  $$(".provider-card").forEach((el) => {
    el.classList.toggle("selected", el.dataset.key === key);
  });
  // 联动网址：若当前网址为空或等于其它 provider 默认值，则更新为这个 provider 的默认值
  const curUrl = ($("#set-weburl").value || "").trim();
  const isDefault = !curUrl || (PROVIDER_CATALOG || PROVIDER_CATALOG_FALLBACK).some((x) => x.url === curUrl);
  if (isDefault) {
    $("#set-weburl").value = p.url || "";
  }
  updateProviderHint();
}

function fillSettingsForm(s) {
  const provider = s.provider || "doubao";
  state.selectedProvider = provider;
  renderProviderGrid(provider);
  // 网址：若服务端返回空，按当前 provider 填默认值，便于用户看到/编辑
  const url = s.web_url || "";
  const p = findProvider(provider);
  $("#set-weburl").value = url || (p ? p.url : "") || "";
  $("#set-timeout").value = s.timeout || 180;
  $("#set-bmode").value = s.browser_mode || "cdp";
  $("#set-cdpurl").value = s.cdp_url || "http://127.0.0.1:9222";
  $("#set-userdata").value = s.user_data_dir || "";
  updateProviderHint();
  // 同步顶栏快捷切换芯片
  updateSwitcherChip(provider, s);
}

// 根据 provider 显示对应登录提示
function updateProviderHint() {
  const provider = state.selectedProvider || "doubao";
  const p = findProvider(provider);
  const hint = $("#provider-hint");
  if (!hint || !p) return;
  if (provider === "custom") {
    hint.textContent = "在下方网址栏填入你要用的网页 AI 聊天页地址（需含 https://），在 Chrome 里登录该页面后用 CDP 连接";
  } else {
    const host = (p.url || "").replace(/^https?:\/\//, "").replace(/\/.*$/, "");
    hint.textContent = `打开 ${host} 登录${p.name}，或点下方「打开登录页」按钮，登录后用 CDP 连接`;
  }
}

// ---------- 顶栏快捷切换芯片 ----------
function updateSwitcherChip(provider, status) {
  const p = findProvider(provider);
  if (!p) return;
  const badge = $("#ai-switcher-badge");
  const name = $("#ai-switcher-name");
  if (badge) {
    badge.textContent = p.initial;
    badge.style.setProperty("--pc", p.color);
  }
  if (name) name.textContent = p.name;
  // 联机状态用 chip-ai 显示，这里只更新名称
  if (status) {
    const online = status.ai_online;
    const connected = status.browser_connected;
    setChip("#chip-ai", online, online ? (connected ? `${p.name} 已连接` : `${p.name} 就绪`) : `${p.name} demo`);
  }
}

function renderSwitcherList(filter) {
  const box = $("#ai-switcher-list");
  if (!box) return;
  const list = PROVIDER_CATALOG || PROVIDER_CATALOG_FALLBACK;
  const q = (filter || "").trim().toLowerCase();
  const filtered = q
    ? list.filter((p) => (p.name + " " + (p.vendor || "") + " " + p.key).toLowerCase().includes(q))
    : list;
  box.innerHTML = "";
  if (!filtered.length) {
    box.innerHTML = '<div class="ai-switcher-empty">无匹配 AI</div>';
    return;
  }
  const current = state.selectedProvider || "doubao";
  filtered.forEach((p) => {
    const item = document.createElement("button");
    item.type = "button";
    item.className = "ai-switcher-item" + (p.key === current ? " current" : "");
    item.innerHTML = `
      <span class="provider-badge sm" style="--pc:${p.color}">${escapeHtml(p.initial)}</span>
      <span class="ai-switcher-item-info">
        <span class="ai-switcher-item-name">${escapeHtml(p.name)}</span>
        <span class="ai-switcher-item-vendor">${escapeHtml(p.vendor || "")}</span>
      </span>
      ${p.key === current ? '<span class="ai-switcher-item-check">✓</span>' : ""}
    `;
    item.addEventListener("click", () => {
      switchProviderQuick(p.key);
      closeSwitcher();
    });
    box.appendChild(item);
  });
}

function openSwitcher() {
  const pop = $("#ai-switcher-popover");
  if (!pop) return;
  pop.hidden = false;
  $("#ai-switcher-btn").setAttribute("aria-expanded", "true");
  renderSwitcherList("");
  const search = $("#ai-switcher-search");
  if (search) { search.value = ""; search.focus(); }
}
function closeSwitcher() {
  const pop = $("#ai-switcher-popover");
  if (!pop) return;
  pop.hidden = true;
  $("#ai-switcher-btn").setAttribute("aria-expanded", "false");
}

async function switchProviderQuick(key) {
  const p = findProvider(key);
  if (!p) return;
  // custom 必须填网址，引导用户去设置弹层
  if (key === "custom") {
    openSettingsModal();
    return;
  }
  const btn = $("#ai-switcher-btn");
  if (btn) btn.disabled = true;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 15000);
  try {
    const res = await fetch("/api/provider/switch", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider: key }),
      signal: ctrl.signal,
    });
    clearTimeout(timer);
    const j = await res.json();
    if (!res.ok || !j.ok) {
      addLog("warn", `切换 ${p.name} 失败：${j.error || res.status}`);
      return;
    }
    state.selectedProvider = key;
    updateSwitcherChip(key, j);
    addLog("info", `已切换到 ${p.name}（${j.web_url}）`, "event");
  } catch (e) {
    clearTimeout(timer);
    const msg = (e && e.name === "AbortError")
      ? `切换 ${p.name} 超时，后端可能正在重建引擎`
      : `切换 ${p.name} 失败：${e}`;
    addLog("warn", msg);
  } finally {
    if (btn) btn.disabled = false;
  }
}

// ---------- 设置弹层开关 ----------
async function openSettingsModal() {
  modal.classList.add("open");
  await ensureProviderCatalog();
  // 先用本地缓存填充表单（即时），再用服务端实际值覆盖
  fillSettingsForm(loadLocalSettings());
  refreshProjectLockStatus();
  try {
    const s = await (await fetch("/api/settings")).json();
    fillSettingsForm(s);
    const tag = s.ai_online ? (s.browser_connected ? "已连接网页 AI" : "网页 AI 就绪（未连接）") : "未启用（demo）";
    $("#set-status").textContent = "当前：" + tag;
  } catch (e) {}
}

$("#btn-settings").addEventListener("click", openSettingsModal);
$("#btn-close-settings").addEventListener("click", () => modal.classList.remove("open"));
modal.addEventListener("click", (e) => { if (e.target === modal) modal.classList.remove("open"); });

// 顶栏快捷切换芯片交互
$("#ai-switcher-btn").addEventListener("click", async (e) => {
  e.stopPropagation();
  const pop = $("#ai-switcher-popover");
  if (pop.hidden) {
    await ensureProviderCatalog();
    openSwitcher();
  } else {
    closeSwitcher();
  }
});
$("#ai-switcher-search").addEventListener("input", (e) => {
  renderSwitcherList(e.target.value);
});
$("#ai-switcher-more").addEventListener("click", () => {
  closeSwitcher();
  openSettingsModal();
});
document.addEventListener("click", (e) => {
  const sw = $("#ai-switcher");
  if (sw && !sw.contains(e.target)) closeSwitcher();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeSwitcher();
});

// ---------- 工程锚点管理：先打开一个工程，之后所有操作都锁定在这个工程里 ----------
async function refreshProjectLockStatus() {
  try {
    const r = await fetch("/api/project/status");
    const j = await r.json();
    const box = $("#project-lock-status");
    const dot = $("#lock-dot");
    const txt = $("#lock-text");
    if (!box || !dot || !txt) return;
    if (j.locked) {
      box.classList.add("locked");
      txt.textContent = `工程已锁定：${j.title || ""}（${j.path || ""}）`;
    } else {
      box.classList.remove("locked");
      txt.textContent = "工程未锁定（开始制作时会在作曲阶段自动创建并锁定）";
    }
  } catch (e) {}
}

$("#btn-open-project") && $("#btn-open-project").addEventListener("click", async () => {
  const input = $("#project-open-path");
  const path = (input.value || "").trim();
  if (!path) {
    addLog("warn", "请先填写 .logicx 工程文件路径", "log");
    return;
  }
  const btn = $("#btn-open-project");
  btn.disabled = true;
  const orig = btn.textContent;
  btn.textContent = "打开中…";
  try {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 15000);
    const res = await fetch("/api/project/open", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
      signal: ctrl.signal,
    });
    clearTimeout(timer);
    const j = await res.json();
    if (j.ok) {
      addLog("info", `已打开并锁定工程：${j.project_title}（${j.project_path}）`, "event");
      refreshProjectLockStatus();
    } else {
      addLog("warn", `打开工程失败：${j.error || "未知错误"}`, "log");
    }
  } catch (e) {
    const msg = (e && e.name === "AbortError") ? "打开工程超时" : `打开工程失败：${e}`;
    addLog("warn", msg, "log");
  } finally {
    btn.disabled = false;
    btn.textContent = orig;
  }
});

$("#btn-reset-project") && $("#btn-reset-project").addEventListener("click", async () => {
  try {
    await fetch("/api/project/reset", { method: "POST" });
    addLog("info", "已重置工程锚点，可开始新一首作品", "event");
    refreshProjectLockStatus();
  } catch (e) {
    addLog("warn", `重置工程失败：${e}`, "log");
  }
});

// 打开登录页按钮：在新标签页打开当前选中 AI 的网址
$("#btn-open-login").addEventListener("click", () => {
  const url = ($("#set-weburl").value || "").trim();
  if (!url || !/^https?:\/\//.test(url)) {
    $("#set-status").textContent = "✗ 网址无效，请先选择 AI 或填写网址";
    return;
  }
  window.open(url, "_blank", "noopener");
  $("#set-status").textContent = "已在新标签页打开登录页：登录后回到此处点「测试连接」";
});

$("#btn-save-settings").addEventListener("click", async () => {
  const btn = $("#btn-save-settings");
  btn.disabled = true;
  const origText = btn.textContent;
  btn.textContent = "保存中…";
  $("#set-status").textContent = "正在保存并重建引擎…";
  try {
    const j = await saveSettingsToBackend();
    if (j && j.ok !== false) {
      const p = findProvider(j.provider);
      const providerName = p ? p.name : j.provider;
      if (j.ai_online) {
        $("#set-status").textContent = `已保存 · ${providerName} 网页 AI 就绪`;
      } else {
        $("#set-status").textContent = "已保存 · demo 模式（未安装 Playwright 或未填网址）";
      }
    } else if (j) {
      $("#set-status").textContent = "✗ " + (j.error || "保存失败");
    }
  } catch (e) {
    const msg = (e && e.name === "AbortError")
      ? "保存超时（15s），后端可能正在重建引擎或浏览器无响应，请稍后重试或检查后端日志"
      : "保存失败：" + e;
    $("#set-status").textContent = msg;
  } finally {
    btn.disabled = false;
    btn.textContent = origText;
  }
});

// 保存设置的通用函数（带超时），供保存按钮与测试连接复用
async function saveSettingsToBackend() {
  const form = collectSettingsForm();
  const body = {
    provider: form.provider,
    web_url: form.web_url || undefined,
    timeout: form.timeout || undefined,
    browser_mode: form.browser_mode,
    cdp_url: form.cdp_url || undefined,
    user_data_dir: form.user_data_dir || undefined,
  };
  saveLocalSettings(form);
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 15000);
  try {
    const res = await fetch("/api/settings", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: ctrl.signal,
    });
    clearTimeout(timer);
    const j = await res.json();
    if (res.ok) {
      state.selectedProvider = j.provider;
      updateSwitcherChip(j.provider, j);
      const p = findProvider(j.provider);
      const providerName = p ? p.name : j.provider;
      setChip("#chip-ai", j.ai_online, j.ai_online ? `${providerName} 就绪` : "demo 模式");
    }
    return j;
  } catch (e) {
    clearTimeout(timer);
    throw e;
  }
}

$("#btn-test-connect").addEventListener("click", async () => {
  const btn = $("#btn-test-connect");
  btn.disabled = true;
  const origText = btn.textContent;
  btn.textContent = "连接中…";
  $("#set-status").textContent = "正在保存并连接浏览器…";
  try {
    // 先保存（等待完成）再测试，确保用最新配置
    await saveSettingsToBackend();
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 30000);
    const res = await fetch("/api/browser/connect", { method: "POST", signal: ctrl.signal });
    clearTimeout(timer);
    const j = await res.json();
    if (j.ok) {
      const p = findProvider(state.selectedProvider);
      $("#set-status").textContent = "✓ 已连接：" + (p ? p.name : "") + " · " + (j.url || "");
      setChip("#chip-ai", true, (p ? p.name : "网页 AI") + " 已连接");
    } else {
      $("#set-status").textContent = "✗ " + (j.error || "连接失败");
    }
  } catch (e) {
    const msg = (e && e.name === "AbortError")
      ? "连接超时，浏览器可能无响应，请检查 Chrome 是否用 --remote-debugging-port=9222 启动"
      : "✗ " + e;
    $("#set-status").textContent = msg;
  } finally {
    btn.disabled = false;
    btn.textContent = origText;
  }
});

// 平台检测 + 启动时初始化 provider 目录与顶栏快捷切换芯片
(async () => {
  try {
    const [hh, ss] = await Promise.allSettled([
      fetch("/api/health").then((r) => r.json()),
      fetch("/api/settings").then((r) => r.json()),
    ]);
    const h = hh.status === "fulfilled" ? hh.value : {};
    const s = ss.status === "fulfilled" ? ss.value : {};
    setChip("#chip-daw", h.daw_platform === "macos", h.daw_platform === "macos" ? "DAW 实控" : "DAW 模拟");
    await ensureProviderCatalog();
    const provider = s.provider || "doubao";
    state.selectedProvider = provider;
    // 合并 health + settings 的在线状态给芯片
    updateSwitcherChip(provider, {
      ai_online: s.ai_online != null ? s.ai_online : h.ai_online,
      browser_connected: s.browser_connected != null ? s.browser_connected : h.browser_connected,
    });
  } catch (e) {}
})();

// ---------- 背景示波器 ----------
(function scope() {
  const canvas = $("#scope");
  const ctx = canvas.getContext("2d");
  let w, h, t = 0;
  function resize() { w = canvas.width = innerWidth; h = canvas.height = innerHeight; }
  resize(); addEventListener("resize", resize);
  function draw() {
    ctx.clearRect(0, 0, w, h);
    ctx.lineWidth = 1.2;
    for (let layer = 0; layer < 3; layer++) {
      ctx.beginPath();
      const amp = 40 + layer * 26;
      const freq = 0.004 + layer * 0.0015;
      const speed = 0.6 + layer * 0.4;
      const yoff = h / 2 + (layer - 1) * 30;
      ctx.strokeStyle = layer === 0 ? "rgba(42,245,182,0.5)"
        : layer === 1 ? "rgba(255,46,136,0.25)" : "rgba(42,245,182,0.12)";
      for (let x = 0; x <= w; x += 2) {
        const y = yoff + Math.sin(x * freq + t * speed) * amp * Math.sin(x * 0.0006 + t * 0.3)
          + Math.sin(x * 0.02 + t) * 6;
        if (x === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }
      ctx.stroke();
    }
    t += 0.05;
    requestAnimationFrame(draw);
  }
  draw();
})();

// ---------- 初始化 ----------
loadRenderHistory();
renderTemplateList();
const btnClearRenders = $("#btn-clear-renders");
if (btnClearRenders) btnClearRenders.addEventListener("click", clearRenderHistory);
const btnSaveTpl = $("#btn-save-template");
if (btnSaveTpl) btnSaveTpl.addEventListener("click", saveCurrentAsTemplate);
const chkSelfEval = $("#chk-self-eval");
if (chkSelfEval) chkSelfEval.addEventListener("change", () => { state.selfEval = chkSelfEval.checked; });

// 视觉模式：手动截图按钮
const btnScreenshot = $("#btn-screenshot");
if (btnScreenshot) btnScreenshot.addEventListener("click", async () => {
  addLog("info", "正在截取 Logic Pro 窗口...", "event");
  try {
    const res = await fetch("/api/screenshot?tag=manual", { method: "POST" });
    const j = await res.json();
    if (j.ok) {
      updateVisualScreenshot(j.path);
      addLog("info", "截图完成", "event");
    } else {
      addLog("warn", `截图失败：${j.error || "未知错误"}`, "event");
    }
  } catch (e) {
    addLog("error", `截图请求失败：${e}`, "log");
  }
});

connect();
