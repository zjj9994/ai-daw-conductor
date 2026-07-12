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
      setChip("#chip-ai", evt.ai_online, evt.ai_online ? "AI 在线" : "AI 离线");
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
      break;
    case "pipeline_progress":
      setStageState(evt.completed, "done");
      break;
    case "pipeline_done":
      addLog("info", `流水线完成：${evt.stages.join(" → ")}`, "event");
      setRunning(false);
      break;
    case "task_finished":
      setRunning(false);
      break;
    case "error":
      addLog("error", evt.message, "log");
      setRunning(false);
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
  node.classList.remove("running", "done");
  if (st === "running") {
    node.classList.add("running");
    node.querySelector(".node-state").textContent = "执行中";
  } else if (st === "done") {
    node.classList.add("done");
    node.querySelector(".node-state").textContent = "完成";
  } else {
    node.querySelector(".node-state").textContent = "待命";
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
  resetPipeline();
  state.tracks = [];
  $("#tracks").innerHTML = '<div class="empty">无轨道</div>';
  $("#mixer").innerHTML = '<div class="empty">无混音数据</div>';
  $("#render").innerHTML = '<div class="empty">未导出</div>';
  $("#project-info").innerHTML = '<div class="empty">尚未生成作品</div>';
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
    if (!res.ok) addLog("error", `请求失败：${res.status}`, "log");
  } catch (e) {
    addLog("error", String(e), "log");
    setRunning(false);
  }
});

$("#btn-cancel").addEventListener("click", async () => {
  try { await fetch("/api/cancel", { method: "POST" }); } catch (e) {}
  addLog("warn", "已请求取消", "log");
});

// ---------- 设置 ----------
const modal = $("#modal-settings");
$("#btn-settings").addEventListener("click", async () => {
  modal.classList.add("open");
  try {
    const s = await (await fetch("/api/settings")).json();
    $("#set-provider").value = s.provider || "doubao";
    $("#set-baseurl").value = s.base_url || "";
    $("#set-model").value = s.model || "";
    $("#set-status").textContent = s.ai_online ? "当前：AI 在线" : "当前：AI 离线（demo 模式）";
  } catch (e) {}
});
$("#btn-close-settings").addEventListener("click", () => modal.classList.remove("open"));
modal.addEventListener("click", (e) => { if (e.target === modal) modal.classList.remove("open"); });

$("#btn-save-settings").addEventListener("click", async () => {
  const body = {
    provider: $("#set-provider").value,
    base_url: $("#set-baseurl").value.trim() || undefined,
    model: $("#set-model").value.trim() || undefined,
    api_key: $("#set-apikey").value.trim() || undefined,
    temperature: parseFloat($("#set-temp").value) || undefined,
  };
  try {
    const res = await fetch("/api/settings", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const j = await res.json();
    $("#set-status").textContent = j.ai_online ? "已保存 · AI 在线" : "已保存 · AI 离线（demo 模式）";
    setChip("#chip-ai", j.ai_online, j.ai_online ? "AI 在线" : "AI 离线");
  } catch (e) {
    $("#set-status").textContent = "保存失败：" + e;
  }
});

// 平台检测
fetch("/api/health").then((r) => r.json()).then((h) => {
  setChip("#chip-daw", h.daw_platform === "macos", h.daw_platform === "macos" ? "DAW 实控" : "DAW 模拟");
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

connect();
