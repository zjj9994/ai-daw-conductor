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
    provider: $("#set-provider").value,
    web_url: $("#set-weburl").value.trim(),
    timeout: parseFloat($("#set-timeout").value) || 180,
    browser_mode: $("#set-bmode").value,
    cdp_url: $("#set-cdpurl").value.trim(),
    user_data_dir: $("#set-userdata").value.trim(),
  };
}

// 各 provider 的默认网址（与后端 DEFAULT_URLS 保持一致）
const PROVIDER_DEFAULT_URLS = {
  doubao: "https://www.doubao.com/chat/",
  kimi: "https://kimi.moonshot.cn/",
  qwen: "https://tongyi.aliyun.com/qianwen/",
  zhipu: "https://chatglm.cn/main/detail/",
  custom: "",
};

function fillSettingsForm(s) {
  $("#set-provider").value = s.provider || "doubao";
  // 网址：若服务端返回空，按当前 provider 填默认值，便于用户看到/编辑
  const url = s.web_url || "";
  $("#set-weburl").value = url || PROVIDER_DEFAULT_URLS[s.provider || "doubao"] || "";
  $("#set-timeout").value = s.timeout || 180;
  $("#set-bmode").value = s.browser_mode || "cdp";
  $("#set-cdpurl").value = s.cdp_url || "http://127.0.0.1:9222";
  $("#set-userdata").value = s.user_data_dir || "";
  updateProviderHint();
}

// 切换 provider 时联动网址：若当前网址是某 provider 的默认值或为空，则更新为新 provider 的默认值
function onProviderChange() {
  const provider = $("#set-provider").value;
  const curUrl = $("#set-weburl").value.trim();
  // 若当前网址等于任意一个已知 provider 的默认值，或为空，则视为「未自定义」可安全替换
  const isDefault = !curUrl || Object.values(PROVIDER_DEFAULT_URLS).includes(curUrl);
  if (isDefault) {
    $("#set-weburl").value = PROVIDER_DEFAULT_URLS[provider] || "";
  }
  updateProviderHint();
}

// 根据 provider 显示对应登录提示
function updateProviderHint() {
  const provider = $("#set-provider").value;
  const hint = $("#provider-hint");
  if (!hint) return;
  const tips = {
    doubao: "打开 doubao.com 登录豆包，或在 Chrome 里登录后用 CDP 连接",
    kimi: "打开 kimi.moonshot.cn 登录 Kimi，或在 Chrome 里登录后用 CDP 连接",
    qwen: "打开 tongyi.aliyun.com 登录通义千问，或在 Chrome 里登录后用 CDP 连接",
    zhipu: "打开 chatglm.cn 登录智谱清言，或在 Chrome 里登录后用 CDP 连接",
    custom: "在下方网址栏填入你要用的网页 AI 聊天页地址（需含 https://），在 Chrome 里登录该页面后用 CDP 连接",
  };
  hint.textContent = tips[provider] || "";
}

$("#btn-settings").addEventListener("click", async () => {
  modal.classList.add("open");
  // 先用本地缓存填充表单（即时），再用服务端实际值覆盖
  fillSettingsForm(loadLocalSettings());
  try {
    const s = await (await fetch("/api/settings")).json();
    fillSettingsForm(s);
    const tag = s.ai_online ? (s.browser_connected ? "已连接网页 AI" : "网页 AI 就绪（未连接）") : "未启用（demo）";
    $("#set-status").textContent = "当前：" + tag;
  } catch (e) {}
});
$("#set-provider").addEventListener("change", onProviderChange);
$("#btn-close-settings").addEventListener("click", () => modal.classList.remove("open"));
modal.addEventListener("click", (e) => { if (e.target === modal) modal.classList.remove("open"); });

$("#btn-save-settings").addEventListener("click", async () => {
  const form = collectSettingsForm();
  const body = {
    provider: form.provider,
    web_url: form.web_url || undefined,
    timeout: form.timeout || undefined,
    browser_mode: form.browser_mode,
    cdp_url: form.cdp_url || undefined,
    user_data_dir: form.user_data_dir || undefined,
  };
  saveLocalSettings(form);  // 本地持久化，下次打开自动回填
  try {
    const res = await fetch("/api/settings", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const j = await res.json();
    if (!res.ok) {
      $("#set-status").textContent = "✗ " + (j.error || "保存失败");
      return;
    }
    const providerName = { doubao: "豆包", kimi: "Kimi", qwen: "通义千问", zhipu: "智谱清言", custom: "自定义" }[j.provider] || j.provider;
    if (j.ai_online) {
      $("#set-status").textContent = `已保存 · ${providerName} 网页 AI 就绪`;
    } else {
      $("#set-status").textContent = "已保存 · demo 模式（未安装 Playwright 或未填网址）";
    }
    setChip("#chip-ai", j.ai_online, j.ai_online ? `${providerName} 就绪` : "demo 模式");
  } catch (e) {
    $("#set-status").textContent = "保存失败：" + e;
  }
});

$("#btn-test-connect").addEventListener("click", async () => {
  $("#set-status").textContent = "正在连接浏览器…";
  // 先保存再测试，确保用最新配置
  $("#btn-save-settings").click();
  try {
    const res = await fetch("/api/browser/connect", { method: "POST" });
    const j = await res.json();
    if (j.ok) {
      $("#set-status").textContent = "✓ 已连接：" + (j.url || j.provider);
      setChip("#chip-ai", true, "已连接网页 AI");
    } else {
      $("#set-status").textContent = "✗ " + (j.error || "连接失败");
    }
  } catch (e) {
    $("#set-status").textContent = "✗ " + e;
  }
});

// 平台检测
fetch("/api/health").then((r) => r.json()).then((h) => {
  setChip("#chip-daw", h.daw_platform === "macos", h.daw_platform === "macos" ? "DAW 实控" : "DAW 模拟");
  setChip("#chip-ai", h.ai_online, h.ai_online ? "网页 AI 就绪" : "demo 模式");
}).catch(() => {});

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
