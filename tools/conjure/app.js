"use strict";
const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const state = { clips: [], clip: null, detail: null, viewVersion: null, compareVersion: 0, sprites: [], addSprite: null, job: null, pollers: {} };
const vA = $("#vAfter"), vB = $("#vBefore"), canvas = $("#overlay"), ctx = canvas.getContext("2d");

async function api(path, opts = {}) {
  if (opts.json !== undefined) {
    opts = { ...opts, method: opts.method || "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(opts.json) };
  }
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok && ![202, 403].includes(r.status)) throw new Error(data.error || r.statusText);
  return { httpStatus: r.status, ...data };
}

/* ---------- tabs ---------- */
$$(".tabs").forEach((nav) => nav.addEventListener("click", (e) => {
  const tab = e.target.dataset.tab; if (!tab) return;
  nav.querySelectorAll("button").forEach((b) => b.classList.toggle("active", b === e.target));
  const side = nav.parentElement;
  side.querySelectorAll(".panel").forEach((p) => p.classList.toggle("active", p.dataset.panel === tab));
  if (tab === "calls") loadCalls();
  if (tab === "provenance") loadProvenance();
  if (tab === "library") loadSprites();
}));
function showTab(group, tab) { const b = document.querySelector(`.tabs[data-group="${group}"] button[data-tab="${tab}"]`); if (b) b.click(); }

/* ---------- clips ---------- */
async function loadClips() {
  const data = await api("clips");
  state.clips = data.clips;
  const jobsByClip = Object.fromEntries((data.jobs || []).map((j) => [j.clip_id, j]));
  $("#clipList").innerHTML = data.clips.map((c) => {
    const job = jobsByClip[c.clip_id];
    const status = c.status === "ready" ? `<span class="pill ok">ready</span>` :
      `<span class="pill warn">${esc(job ? `${job.stage || "queued"} ${Math.round(job.progress * 100)}%` : c.status)}</span>`;
    return `<div class="card ${state.clip === c.clip_id ? "active" : ""}" data-clip="${c.clip_id}">
      ${c.status === "ready" ? `<img src="${c.thumb_url}" alt="">` : `<img alt="">`}
      <div class="body"><div class="title">${esc(c.camera_id || "upload")} · ${esc(c.title)}</div>
      ${status}${c.head ? `<span class="pill">v${c.head}</span>` : ""}
      <div class="cap">${esc(c.caption || "")}</div></div></div>`;
  }).join("") || `<p class="hint">No clips yet. Use Search to load one from the archive.</p>`;
  $$("#clipList .card").forEach((el) => el.onclick = () => {
    const c = state.clips.find((x) => x.clip_id === el.dataset.clip);
    if (c && c.status === "ready") openClip(c.clip_id);
  });
  if (!state.clip) {
    const ready = data.clips.find((c) => c.status === "ready");
    if (ready) openClip(ready.clip_id);
    else $("#empty").textContent = data.clips.length ? "Preparing clips: downloading, detecting objects, building the clean background plate…" : "No clips yet. Search the archive on the left.";
  }
  const preparing = data.clips.some((c) => c.status !== "ready") || (data.jobs || []).some((j) => j.kind === "prepare");
  clearTimeout(state.pollers.clips);
  if (preparing) state.pollers.clips = setTimeout(loadClips, 2000);
}

async function openClip(clipId, keepView = false) {
  const d = await api(`clips/${clipId}`);
  state.clip = clipId; state.detail = d;
  $("#empty").hidden = true; $("#editor").hidden = false;
  const m = d.meta;
  $("#clipTitle").textContent = `${m.camera_id || "Uploaded clip"} · ${m.title}`;
  $("#clipMeta").textContent = `${m.duration}s · ${m.orig_w}×${m.orig_h} → ${m.work_w}×${m.work_h} · ${m.fps.toFixed(1)} fps · ${d.tracks.length} tracked objects · ${m.camera_static ? "static camera" : "camera motion compensated"}`;
  if (!keepView) { state.viewVersion = d.head; state.compareVersion = 0; }
  setVideos();
  renderChips();
  renderHistory();
  loadProvenance();
  $$("#clipList .card").forEach((el) => el.classList.toggle("active", el.dataset.clip === clipId));
  $("#ask").hidden = true;
}

function version(n) { return state.detail.versions.find((v) => v.version === n); }

function setVideos() {
  const after = version(state.viewVersion) || version(state.detail.head);
  const before = version(state.compareVersion) || version(0);
  const t = vA.currentTime || 0;
  if (vA.getAttribute("src") !== after.url) vA.src = after.url;
  if (vB.getAttribute("src") !== before.url) vB.src = before.url;
  $("#lblAfter").textContent = `v${after.version} · ${after.label}`;
  $("#lblBefore").textContent = before.version === 0 ? "Original" : `v${before.version} · ${before.label}`;
  vA.onloadedmetadata = () => { vA.currentTime = Math.min(t, vA.duration - 0.05); vA.play().catch(() => {}); };
}

/* ---------- playback + sync ---------- */
const frameOf = (t) => Math.max(0, Math.min(state.detail.meta.n_frames - 1, Math.round(t * state.detail.meta.fps)));
function sync() {
  if (Math.abs(vB.currentTime - vA.currentTime) > 0.06) vB.currentTime = vA.currentTime;
  if (!vA.paused && vB.paused) vB.play().catch(() => {});
  if (vA.paused && !vB.paused) vB.pause();
}
function tick() {
  if (state.detail && vA.duration) {
    sync();
    $("#scrub").value = Math.round((vA.currentTime / vA.duration) * 1000);
    $("#timeLbl").textContent = `${vA.currentTime.toFixed(2)}s · f${frameOf(vA.currentTime)}`;
    $("#playBtn").textContent = vA.paused ? "▶" : "❚❚";
    draw();
  }
  requestAnimationFrame(tick);
}
requestAnimationFrame(tick);
$("#playBtn").onclick = () => (vA.paused ? vA.play() : vA.pause());
$("#scrub").oninput = (e) => { vA.pause(); vA.currentTime = (e.target.value / 1000) * vA.duration; vB.currentTime = vA.currentTime; };
$("#split").oninput = (e) => $("#viewer").style.setProperty("--split", `${e.target.value}%`);
$$(".modes button").forEach((b) => b.onclick = () => {
  $$(".modes button").forEach((x) => x.classList.toggle("active", x === b));
  $("#viewer").className = `viewer ${b.dataset.mode}`;
});

/* ---------- overlay: boxes + click-to-select ---------- */
function boxAt(track, f) { return f < track.first || f > track.last ? null : track.boxes[f - track.first]; }
function visibleTracks(f) {
  const ex = new Set(currentExcluded());
  return state.detail.tracks.filter((t) => !ex.has(t.id)).map((t) => [t, boxAt(t, f)]).filter(([, b]) => b);
}
function currentExcluded() { const v = version(state.viewVersion); return (v && v.excluded_tracks) || []; }
function draw() {
  const r = canvas.getBoundingClientRect();
  if (canvas.width !== Math.round(r.width) || canvas.height !== Math.round(r.height)) { canvas.width = Math.round(r.width); canvas.height = Math.round(r.height); }
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (!$("#showBoxes").checked || !vA.paused) return;
  const m = state.detail.meta, sx = canvas.width / m.work_w, sy = canvas.height / m.work_h;
  ctx.lineWidth = 1.5; ctx.font = "11px system-ui";
  for (const [t, b] of visibleTracks(frameOf(vA.currentTime))) {
    const hot = state.hover === t.id;
    ctx.strokeStyle = hot ? "#c792ea" : "rgba(95,212,255,.75)";
    ctx.strokeRect(b[0] * sx, b[1] * sy, (b[2] - b[0]) * sx, (b[3] - b[1]) * sy);
    if (hot || (b[2] - b[0]) * sx > 40) {
      ctx.fillStyle = hot ? "#c792ea" : "rgba(95,212,255,.85)";
      ctx.fillText(`${t.label} #${t.id}`, b[0] * sx + 2, b[1] * sy - 3);
    }
  }
}
function pointToWork(e) {
  const r = canvas.getBoundingClientRect(), m = state.detail.meta;
  return { nx: (e.clientX - r.left) / r.width, ny: (e.clientY - r.top) / r.height, x: ((e.clientX - r.left) / r.width) * m.work_w, y: ((e.clientY - r.top) / r.height) * m.work_h };
}
function trackAt(p) {
  const hits = visibleTracks(frameOf(vA.currentTime)).filter(([, b]) => p.x >= b[0] - 4 && p.x <= b[2] + 4 && p.y >= b[1] - 4 && p.y <= b[3] + 4);
  hits.sort((a, b) => (a[1][2] - a[1][0]) * (a[1][3] - a[1][1]) - (b[1][2] - b[1][0]) * (b[1][3] - b[1][1]));
  return hits.length ? hits[0][0] : null;
}
canvas.addEventListener("mousemove", (e) => { if (!state.detail) return; const t = trackAt(pointToWork(e)); state.hover = t ? t.id : null; });
canvas.addEventListener("click", (e) => {
  if (!state.detail) return;
  const p = pointToWork(e);
  vA.pause();
  if (state.addSprite) {
    submitEdit({ click_point: [p.nx, p.ny], action: "add", new_object: state.addSprite.label, sprite: state.addSprite.name });
    setAddSprite(null);
    return;
  }
  const t = trackAt(p);
  if (!t) { hidePopover(); return; }
  showPopover(e, t);
});
function showPopover(e, track) {
  const pop = $("#popover"), vr = $("#viewer").getBoundingClientRect();
  const labels = [...new Set(state.sprites.map((s) => s.label))];
  pop.innerHTML = `<div class="ttl">${esc(track.label)} #${track.id} — tracked f${track.first}–f${track.last}</div>
    <button data-a="remove">Remove</button><button data-a="blur">Blur / redact</button>
    <div class="row"><select id="popRepl">${labels.map((l) => `<option>${esc(l)}</option>`).join("")}</select><button data-a="replace">Replace</button></div>
    <button data-a="cancel">Cancel</button>`;
  pop.style.left = `${Math.min(e.clientX - vr.left + 8, vr.width - 190)}px`;
  pop.style.top = `${Math.min(e.clientY - vr.top + 8, vr.height - 170)}px`;
  pop.hidden = false;
  pop.onclick = (ev) => {
    const a = ev.target.dataset.a; if (!a) return;
    hidePopover();
    if (a === "cancel") return;
    const body = { track_ids: [track.id], action: a, method: "user_click" };
    if (a === "replace") body.new_object = $("#popRepl").value;
    submitEdit(body);
  };
}
function hidePopover() { $("#popover").hidden = true; }

/* ---------- commands + jobs ---------- */
function renderChips() {
  const labels = state.detail.meta.labels || [];
  const ex = [];
  if (labels.includes("person")) ex.push("remove the person on the left", "blur the largest person");
  if (labels.includes("truck")) ex.push("replace the truck with a bus");
  else if (labels.includes("car")) ex.push("replace the car with a truck");
  if (labels.includes("bicycle")) ex.push("remove the bicycle");
  ex.push("add a traffic cone on the right", "erase all cars");
  $("#cmdChips").innerHTML = ex.slice(0, 6).map((x) => `<span class="chip">${esc(x)}</span>`).join("");
  $$("#cmdChips .chip").forEach((c) => c.onclick = () => { $("#cmdInput").value = c.textContent; $("#cmdInput").focus(); });
}
$("#cmdForm").onsubmit = (e) => {
  e.preventDefault();
  const text = $("#cmdInput").value.trim();
  if (text) submitEdit({ command_text: text });
};
$("#anonBtn").onclick = async () => {
  if (!state.clip) return;
  const r = await api("anonymize", { json: { clip_id: state.clip, watermark: $("#wmToggle").checked } });
  startJob(r.job_id, "One-click anonymize");
};

async function submitEdit(extra) {
  if (!state.clip) return;
  if (state.viewVersion !== state.detail.head) {
    if (!confirm(`You are viewing v${state.viewVersion}. Edits apply to the current head (v${state.detail.head}). Continue?`)) return;
  }
  $("#ask").hidden = true; $("#qa").hidden = true;
  const body = { clip_id: state.clip, time: vA.currentTime, watermark: $("#wmToggle").checked, command_text: "", ...extra };
  if (extra.command_text && !extra.track_ids) state.lastCommand = extra.command_text;
  try {
    const r = await api("edit", { json: body });
    if (r.refused) return showRefusal(r);
    startJob(r.job_id, body.command_text || `${extra.action} (click)`);
  } catch (err) { showAsk(`<b>Error:</b> ${esc(err.message)}`); }
}

function startJob(jobId, title) {
  state.job = jobId;
  $("#progress").hidden = false;
  $("#progressMsg").textContent = title;
  poll(jobId);
}
async function poll(jobId) {
  let j;
  try { j = await api(`jobs/${jobId}`); } catch (e) { setTimeout(() => poll(jobId), 1500); return; }
  $("#stages").innerHTML = j.stages.map((s, i) => {
    const idx = j.stages.indexOf(j.stage);
    const cls = j.status === "done" || i < idx ? "done" : i === idx && j.status === "running" ? "now" : "";
    return `<span class="stage ${cls}">${esc(s)}</span>`;
  }).join("");
  $("#barFill").style.width = `${Math.round(j.progress * 100)}%`;
  if (j.status === "queued" || j.status === "running") { setTimeout(() => poll(jobId), 600); return; }
  if (j.status === "error") { $("#progressMsg").textContent = `Failed: ${j.error}`; return; }
  if (j.status === "needs_input") { $("#progress").hidden = true; return showQuestion(j.result); }
  $("#progressMsg").textContent = `Done in ${j.elapsed_s}s`;
  setTimeout(() => ($("#progress").hidden = true), 2500);
  await openClip(state.clip);
  state.viewVersion = j.result.version; state.compareVersion = 0;
  setVideos(); renderHistory();
  showQA(j.result);
  loadClips();
}

function showQuestion(res) {
  let html = `<b>Conjure needs you to choose:</b> ${esc(res.question)}`;
  if (res.candidates && res.candidates.length) {
    html += `<div class="cands">${res.candidates.map((c) => `<button class="cand" data-id="${c.track_id}" title="${esc(c.label)} #${c.track_id}"><img src="${c.thumb_url}" alt=""></button>`).join("")}</div>`;
  }
  showAsk(html);
  $$("#ask .cand").forEach((b) => b.onclick = () => {
    $("#ask").hidden = true;
    if (res.anchor !== undefined) vA.currentTime = res.anchor / state.detail.meta.fps;
    submitEdit({ track_ids: [Number(b.dataset.id)], intent: res.intent, parser: res.parser, method: "nl_detection+user_pick", command_text: state.lastCommand || "", action: res.intent && res.intent.action });
  });
}
function showAsk(html, refused = false) { const el = $("#ask"); el.innerHTML = html; el.className = refused ? "refused" : ""; el.hidden = false; }
function showRefusal(r) {
  showAsk(`<b>Refused.</b> ${esc(r.reason)} The refusal was recorded in this clip's provenance log (entry ${r.provenance_entry.sequence}).`, true);
  loadProvenance();
}
function showQA(res) {
  const q = res.qa, el = $("#qa");
  const e = res.provenance_entry;
  let html = `<b>${esc(e.description || e.operation)}</b> → v${res.version}. Signed provenance entry #${e.sequence} <span class="mono">${esc(e.entry_hash.slice(0, 19))}…</span>`;
  if (q) {
    const vs = q.baseline_score !== undefined ? ` vs ${q.baseline_score} for the unedited original` : "";
    html += q.ok ? `<div style="margin-top:6px">Cosmos3-Reason blind quality review: <span class="pill ${q.natural ? "ok" : "warn"}">${q.natural ? "no worse than the original" : "new artifacts likely"}</span> score ${q.score ?? "?"}${vs} — ${esc(q.summary)}${q.issues && q.issues.length ? `<br><span class="muted">${esc(q.issues.join("; "))}</span>` : ""}</div>`
      : `<div class="muted" style="margin-top:6px">${esc(q.summary)}</div>`;
  }
  el.innerHTML = html; el.hidden = false;
}

/* ---------- history ---------- */
function renderHistory() {
  const d = state.detail;
  $("#history").innerHTML = [...d.versions].reverse().map((v) => {
    const q = v.qa && v.qa.ok ? `<span class="pill ${v.qa.natural ? "ok" : "warn"}">QA ${v.qa.score ?? "?"}</span>` : "";
    return `<div class="ver ${v.version === d.head ? "head" : ""}">
      <div class="top"><b>v${v.version}</b><span class="muted">${esc(v.created.slice(11, 19))}Z</span></div>
      <div>${esc(v.label)} ${v.version === d.head ? '<span class="pill ok">head</span>' : ""}${v.version === state.viewVersion ? '<span class="pill">viewing</span>' : ""}${q}</div>
      <div class="mono muted">sha256:${esc(v.artifact.slice(0, 16))}…${v.parent !== null && v.parent !== undefined ? ` · from v${v.parent}` : ""}</div>
      <div class="actions">
        <button data-view="${v.version}">View vs original</button>
        ${v.parent !== null && v.parent !== undefined ? `<button data-cmp="${v.version}">Compare with v${v.parent}</button>` : ""}
        ${v.version !== d.head ? `<button data-revert="${v.version}">Revert to v${v.version}</button>` : ""}
      </div></div>`;
  }).join("");
  $$("#history [data-view]").forEach((b) => b.onclick = () => { state.viewVersion = +b.dataset.view; state.compareVersion = 0; setVideos(); renderHistory(); });
  $$("#history [data-cmp]").forEach((b) => b.onclick = () => { const v = version(+b.dataset.cmp); state.viewVersion = v.version; state.compareVersion = v.parent; setVideos(); renderHistory(); });
  $$("#history [data-revert]").forEach((b) => b.onclick = async () => {
    await api(`clips/${state.clip}/revert`, { json: { version: +b.dataset.revert } });
    await openClip(state.clip);
  });
}

/* ---------- provenance ---------- */
async function loadProvenance() {
  if (!state.clip) return;
  const p = await api(`provenance/${state.clip}`);
  const v = p.verification;
  $("#verify").innerHTML = `<div class="verify ${v.ok ? "ok" : "bad"}">${v.ok ? "✓ Verified" : "✗ Verification failed"}: ${v.entries} entries, hash chain + ${esc(v.algorithm)} signatures + artifact hashes checked. Key id <span class="mono">${esc(v.key_id)}</span>${v.problems.length ? `<br>${v.problems.map(esc).join("<br>")}` : ""}</div>`;
  $("#provDownload").href = `provenance/${state.clip}.jsonl`;
  $("#provList").innerHTML = [...p.entries].reverse().map((e) => {
    const t = e.target || {};
    const mu = e.models_used || {};
    const rows = [
      ["when", e.timestamp], ["command", e.command_text ? `"${e.command_text}"` : null], ["change", e.description || e.note || e.reason],
      ["target", t.object_class ? `${t.object_class}${t.track_ids ? ` (track ${t.track_ids.join(", ")})` : ""} via ${t.resolution_method}` : null],
      ["bbox", t.bbox ? `[${t.bbox.join(", ")}] @ f${t.anchor_frame} (original px)` : null],
      ["frames", t.frames_affected ? `${t.frames_affected[0]}–${t.frames_affected[1]}` : null],
      ["detector", mu.detector], ["segmenter", mu.segmenter && e.operation !== "ingest" ? mu.segmenter : null], ["editor", mu.editor], ["parser", mu.nl_parser],
      ["reviewer", mu.reviewer], ["input", e.input_artifact], ["output", e.output_artifact],
      ["prev", e.prev_entry_hash], ["hash", e.entry_hash], ["signature", e.signature],
    ].filter(([, val]) => val);
    return `<div class="entry ${esc(e.operation)}"><div class="op">#${e.sequence} ${esc(e.operation)}</div><dl>${rows.map(([k, val]) => `<dt>${k}</dt><dd>${esc(val)}</dd>`).join("")}</dl></div>`;
  }).join("");
}

/* ---------- search ---------- */
const SEARCH_EXAMPLES = ["person in yellow vest", "red SUV", "delivery truck", "cyclist in the bike lane", "bus at the intersection", "person with an umbrella"];
$("#searchChips").innerHTML = SEARCH_EXAMPLES.map((x) => `<span class="chip">${esc(x)}</span>`).join("");
$$("#searchChips .chip").forEach((c) => c.onclick = () => { $("#searchQuery").value = c.textContent; $("#searchForm").requestSubmit(); });
$("#searchForm").onsubmit = async (e) => {
  e.preventDefault();
  const q = $("#searchQuery").value.trim(); if (!q) return;
  $("#searchResults").innerHTML = `<p class="hint">Searching the VAST index…</p>`;
  try {
    const r = await api("search", { json: { query: q, camera_id: $("#searchCamera").value, object_class: $("#searchClass").value.trim() } });
    $("#searchResults").innerHTML = (r.results.length ? `<p class="hint">${r.results.length} clips${r.filter ? ` containing a ${esc(r.filter)}` : ""}. Click to load into the editor.</p>` : `<p class="hint">No matches. Check the camera filter, or try a broader query.</p>`) +
      r.results.map((x) => `<div class="card" data-src="${esc(x.source)}">
        <img src="${esc(x.thumb_url)}" loading="lazy" alt="">
        <div class="body"><div class="title">${esc(x.camera_id)} · ${x.segment[0] ?? "?"}–${x.segment[1] ?? "?"}s ${x.loaded ? '<span class="pill ok">loaded</span>' : ""}</div>
        <div class="cap">${esc(x.caption)}</div>
        <div>${Object.entries(x.object_counts).sort((a, b) => b[1] - a[1]).slice(0, 4).map(([k, n]) => `<span class="pill">${esc(k)} ${n}</span>`).join("")}${x.similarity ? `<span class="pill">sim ${Number(x.similarity).toFixed(2)}</span>` : ""}</div></div></div>`).join("");
    $$("#searchResults .card").forEach((el) => el.onclick = () => loadSource(el.dataset.src));
  } catch (err) { $("#searchResults").innerHTML = `<p class="hint">Search failed: ${esc(err.message)}</p>`; }
};
async function loadSource(source) {
  const r = await api("clips", { json: { source } });
  showTab("left", "clips");
  if (r.status === "ready") return openClip(r.clip_id);
  state.clip = null;
  $("#editor").hidden = true; $("#empty").hidden = false;
  $("#empty").textContent = "Preparing clip: downloading, detecting and tracking objects, building the clean background plate…";
  waitForClip(r.clip_id);
  loadClips();
}
async function waitForClip(clipId) {
  const d = await api("clips");
  const c = d.clips.find((x) => x.clip_id === clipId);
  if (c && c.status === "ready") return openClip(clipId);
  setTimeout(() => waitForClip(clipId), 2000);
}
$("#uploadInput").onchange = async (e) => {
  const f = e.target.files[0]; if (!f) return;
  const fd = new FormData(); fd.append("file", f);
  $("#empty").textContent = "Uploading and preparing your clip…";
  const r = await api("clips", { method: "POST", body: fd });
  waitForClip(r.clip_id); loadClips();
};

/* ---------- library ---------- */
async function loadSprites() {
  const r = await api("sprites");
  state.sprites = r.sprites;
  $("#spriteGrid").innerHTML = r.sprites.map((s) => `<div class="sprite ${state.addSprite && state.addSprite.name === s.name ? "active" : ""}" data-name="${esc(s.name)}"><img src="${esc(s.url)}" alt=""><div>${esc(s.label.replace("_", " "))}</div><div class="muted">${s.kind === "cutout" ? "archive cutout" : "procedural"}</div></div>`).join("");
  $$("#spriteGrid .sprite").forEach((el) => el.onclick = () => setAddSprite(state.sprites.find((s) => s.name === el.dataset.name)));
}
function setAddSprite(s) {
  state.addSprite = s;
  $("#addMode").innerHTML = s ? `Placing <b>${esc(s.label.replace("_", " "))}</b>: click where it should stand <button id="addCancel">cancel</button>` : "";
  if (s) $("#addCancel").onclick = () => setAddSprite(null);
  $$("#spriteGrid .sprite").forEach((el) => el.classList.toggle("active", s && el.dataset.name === s.name));
}

/* ---------- model calls ---------- */
async function loadCalls() {
  const r = await api("calls");
  const stats = Object.entries(r.stats);
  $("#calls").innerHTML = `<p class="hint">Weave tracing: ${r.weave.enabled ? "on" : "off (calls still logged here)"}.</p>
    <table class="calls"><tr><th>op</th><th>calls</th><th>err</th><th>avg ms</th></tr>${stats.map(([k, s]) => `<tr><td>${esc(k)}</td><td>${s.count}</td><td>${s.errors}</td><td>${s.avg_ms}</td></tr>`).join("")}</table>
    <h4>Recent</h4><table class="calls">${r.calls.slice(0, 25).map((c) => `<tr><td>${esc(c.op)}</td><td>${c.ok ? "ok" : "fail"}</td><td>${c.ms}ms</td><td class="muted">${esc(c.label || "")}</td></tr>`).join("")}</table>`;
}

loadClips();
loadSprites();
