/* AI-DAW-Conductor 前端逻辑：WebSocket 事件流 + UI 更新 + 背景示波器 */
"use strict";

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

// ---------- 状态 ----------
const state = {
  mode: "pipeline",       // pipeline | stage
  stage: "compose",
  running: false,
  tracks: [],
  logCount: 0,
  pollTimer: null,        // 任务状态轮询计时器
  lastPrompt: "",         // 最近一次创作指令（用于重试）
};

const STAGE_NAMES = { compose: "作曲", arrange: "编曲", mix: "混音", master: "母带" };

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
  addLog("info", `开始任务（${state.mode === "pipeline" ? "全流程" : state.stage}）`, "event");

  try {
    const url = state.mode === "pipeline" ? "/api/pipeline" : "/api/stage";
    const body = state.mode === "pipeline"
      ? { prompt }
      : { stage: state.stage, prompt, bpm: 100 };
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
function fillSettingsForm(s) {
  $("#set-provider").value = s.provider || "doubao";
  $("#set-weburl").value = s.web_url || "";
  $("#set-timeout").value = s.timeout || 180;
  $("#set-bmode").value = s.browser_mode || "cdp";
  $("#set-cdpurl").value = s.cdp_url || "http://127.0.0.1:9222";
  $("#set-userdata").value = s.user_data_dir || "";
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
    $("#set-status").textContent = j.ai_online ? "已保存 · 网页 AI 就绪" : "已保存 · demo 模式（未安装 Playwright）";
    setChip("#chip-ai", j.ai_online, j.ai_online ? "网页 AI 就绪" : "demo 模式");
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

connect();
