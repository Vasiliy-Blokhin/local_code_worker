"use strict";
// Code Worker — клиент. Без сборки и зависимостей.

const $ = (s, el = document) => el.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const LS = {
  get: (k, d) => { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch { return d; } },
  set: (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} },
};

const state = {
  projects: [], project: null, files: [], sessions: [], session: null,
  items: [], seq: 0, meta: {}, rewindFloor: 0, es: null, context: 32768, model: null,
};

// ------------------------------------------------------------------ API
async function api(method, url, body) {
  const r = await fetch(url, {
    method, headers: body ? {"Content-Type": "application/json"} : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (r.status === 401) { showLogin(); throw new Error("Нужен вход"); }
  const ct = r.headers.get("content-type") || "";
  const data = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error((data && data.detail) || r.statusText);
  return data;
}

function showLogin() {
  const d = $("#loginDlg");
  if (!d.open) d.showModal();
}
$("#loginForm").addEventListener("submit", async e => {
  e.preventDefault();
  try {
    await api("POST", "/api/login", {password: e.target.password.value});
    $("#loginDlg").close();
    init();
  } catch (err) { $("#loginErr").textContent = err.message; }
});

// ------------------------------------------------------------ markdown
function md(src) {
  const blocks = [];
  src = src.replace(/```([\w+-]*)\n?([\s\S]*?)(```|$)/g, (_, lang, code) => {
    blocks.push(`<pre><code class="lang-${esc(lang)}">${esc(code.replace(/\n$/, ""))}</code></pre>`);
    return `\u0000${blocks.length - 1}\u0000`;
  });
  const inline = t => esc(t)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<i>$2</i>")
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  const out = [];
  let list = null, para = [], table = [];
  const flushPara = () => { if (para.length) out.push(`<p>${para.map(inline).join("<br>")}</p>`); para = []; };
  const flushList = () => { if (list) out.push(`<${list.t}>${list.items.map(i => `<li>${inline(i)}</li>`).join("")}</${list.t}>`); list = null; };
  const flushTable = () => {
    if (!table.length) return;
    const rows = table.filter(r => !/^\|?\s*:?-{2,}/.test(r)).map(r => r.replace(/^\||\|$/g, "").split("|"));
    out.push("<table>" + rows.map((r, i) => "<tr>" + r.map(c => i ? `<td>${inline(c.trim())}</td>` : `<th>${inline(c.trim())}</th>`).join("") + "</tr>").join("") + "</table>");
    table = [];
  };
  for (const line of src.split("\n")) {
    let m;
    if (/^\u0000\d+\u0000$/.test(line.trim())) { flushPara(); flushList(); flushTable(); out.push(line.trim()); continue; }
    if (/^\s*\|.*\|\s*$/.test(line)) { flushPara(); flushList(); table.push(line.trim()); continue; }
    flushTable();
    if ((m = line.match(/^(#{1,4})\s+(.*)/))) { flushPara(); flushList(); out.push(`<h${m[1].length + 1}>${inline(m[2])}</h${m[1].length + 1}>`); continue; }
    if ((m = line.match(/^\s*([-*]|\d+[.)])\s+(.*)/))) {
      flushPara();
      const t = /\d/.test(m[1]) ? "ol" : "ul";
      if (!list || list.t !== t) { flushList(); list = {t, items: []}; }
      list.items.push(m[2]);
      continue;
    }
    if (!line.trim()) { flushPara(); flushList(); continue; }
    if (list && /^\s{2,}/.test(line)) { list.items[list.items.length - 1] += " " + line.trim(); continue; }
    flushList();
    para.push(line);
  }
  flushPara(); flushList(); flushTable();
  return out.join("").replace(/\u0000(\d+)\u0000/g, (_, i) => blocks[+i]);
}

function diffHtml(d) {
  if (!d) return "";
  return `<div class="diff">${d.split("\n").map(l => {
    const c = l.startsWith("+++") || l.startsWith("---") ? "h" : l.startsWith("+") ? "a" : l.startsWith("-") ? "d" : l.startsWith("@@") ? "h" : "";
    return `<div class="${c}">${esc(l) || " "}</div>`;
  }).join("")}</div>`;
}

// ------------------------------------------------------------- рендер
const T = $("#transcript");
const nodes = [];          // DOM-узлы элементов (индекс = индекс item)
const openTools = new Set();
let pendingRender = new Set(), rafId = 0;

function toolSummary(it) {
  const a = it.args || {};
  if (it.name === "run") return a.command;
  if (it.name === "grep") return `${a.pattern}${a.path ? "  in " + a.path : ""}${a.glob ? "  " + a.glob : ""}`;
  if (it.name === "glob") return `${a.pattern}${a.path ? "  in " + a.path : ""}`;
  if (it.name === "read") return `${a.path}${a.offset ? ":" + a.offset : ""}`;
  return a.path || a.raw || JSON.stringify(a);
}
const ST = {running: "выполняется…", ok: "✓", error: "ошибка", approve: "ждёт подтверждения", denied: "отклонено"};

function renderItem(i) {
  const it = state.items[i];
  let el = nodes[i];
  if (!el) {
    el = document.createElement("div");
    nodes[i] = el;
    T.appendChild(el);
  }
  const running = state.meta.running;
  const last = i === state.items.length - 1;
  el.className = "item " + it.type;
  switch (it.type) {
    case "user": {
      const canRw = !running && it.turn > state.rewindFloor;
      el.innerHTML = `${canRw ? `<button class="btn small rw" title="Откатить файлы и историю к моменту до этого сообщения">↶ Откатить</button>` : ""}${esc(it.text)}`;
      if (canRw) $(".rw", el).onclick = () => rewindTo(it);
      break;
    }
    case "reasoning": {
      // открыт, пока идёт стриминг; после — свёрнут, если пользователь сам не раскрыл
      const open = !it.done || el.dataset.userOpen === "1";
      el.innerHTML = `<details ${open ? "open" : ""}><summary>Размышления${it.done ? "" : "…"} <span class="small">(${it.text.length} симв.)</span></summary><div class="body">${esc(it.text)}</div></details>`;
      const det = $("details", el), body = $(".body", el);
      body.scrollTop = body.scrollHeight;
      $("summary", el).onclick = () => { if (it.done) el.dataset.userOpen = det.open ? "0" : "1"; };
      break;
    }
    case "text":
      el.innerHTML = md(it.text);
      el.classList.toggle("cursor", running && last);
      break;
    case "tool": {
      const open = openTools.has(i) || it.status === "approve";
      const detail = [];
      if (it.name === "edit" && it.status === "approve" && !it.diff) {
        detail.push(diffHtml(`--- ${it.args.path}\n${(it.args.old || "").split("\n").map(l => "-" + l).join("\n")}\n${(it.args.new || "").split("\n").map(l => "+" + l).join("\n")}`));
      }
      if (it.name === "write" && it.status === "approve" && !it.diff) detail.push(`<pre>${esc(it.args.path)}: ${esc(it.args.content)}</pre>`);
      if (it.diff) detail.push(diffHtml(it.diff));
      else if (it.output) detail.push(`<pre>${esc(it.output)}</pre>`);
      el.innerHTML = `<div class="head"><span class="name">${esc(it.name)}</span><span class="arg">${esc(toolSummary(it))}</span><span class="st ${it.status}">${ST[it.status] || it.status}</span></div>`
        + (open && detail.length ? `<div class="detail">${detail.join("")}</div>` : "")
        + (it.status === "approve" ? `<div class="approve-bar"><button class="btn primary" data-d="allow">Разрешить</button><button class="btn" data-d="always">Всегда для «${esc(it.name)}»</button><input placeholder="Комментарий при отказе…"><button class="btn danger" data-d="deny">Отклонить</button></div>` : "");
      $(".head", el).onclick = () => { openTools.has(i) ? openTools.delete(i) : openTools.add(i); renderItem(i); };
      el.querySelectorAll("[data-d]").forEach(b => b.onclick = () => approve(it.id, b.dataset.d, $("input", el).value));
      break;
    }
    case "plan": {
      el.className = "item plan";
      const total = it.steps.length;
      const done = it.steps.filter(s => s.status === "done").length;
      el.innerHTML = `<div class="ph">План (${done}/${total})</div>`;
      const ol = document.createElement("ol");
      it.steps.forEach(step => {
        const icon = {todo: "○", doing: "◐", done: "●", failed: "✕"}[step.status];
        const li = document.createElement("li");
        li.className = `st-${step.status}`;
        li.innerHTML = `<span class="ic">${icon}</span> ${esc(step.text)}`;
        ol.appendChild(li);
      });
      el.appendChild(ol);
      break;
    }
    case "notice":
      el.className = `item notice ${it.level || ""}`;
      el.innerHTML = esc(it.text) + (it.summary ? `<details><summary>Сводка</summary>${esc(it.summary)}</details>` : "");
      break;
  }
}

function scheduleRender(i) {
  pendingRender.add(i);
  if (!rafId) rafId = requestAnimationFrame(() => {
    rafId = 0;
    const stick = T.scrollHeight - T.scrollTop - T.clientHeight < 80;
    for (const j of pendingRender) renderItem(j);
    pendingRender.clear();
    if (stick) T.scrollTop = T.scrollHeight;
  });
}

function renderAll() {
  T.innerHTML = "";
  nodes.length = 0;
  openTools.clear();
  if (!state.items.length) T.appendChild(emptyNode);
  state.items.forEach((_, i) => renderItem(i));
  T.scrollTop = T.scrollHeight;
}
const emptyNode = $("#emptyState");

function renderMeta() {
  const m = state.meta, u = m.usage || {};
  const used = u.ctx_used || 0, ctx = m.context || state.context;
  const pct = Math.min(100, Math.round(used / ctx * 100));
  $("#ctxMeter i").style.width = pct + "%";
  $("#ctxMeter").classList.toggle("high", pct > 70);
  $("#ctxMeter b").textContent = used ? `${(used / 1000).toFixed(1)}k / ${Math.round(ctx / 1000)}k` : "—";
  $("#speed").textContent = u.gen_tps ? `⇢ ${u.prompt_tps} т/с · ⇠ ${u.gen_tps} т/с${u.cached ? ` · кэш ${u.cached}` : ""}` : "";
  const btn = $("#sendBtn");
  btn.textContent = m.running ? "Стоп" : "Отправить";
  btn.classList.toggle("primary", !m.running);
  btn.classList.toggle("danger", !!m.running);
  $("#runState").textContent = m.running ? "агент работает…" : "";
  if (m.settings) { $("#modeSel").value = m.settings.mode; $("#effortSel").value = m.settings.effort; }
  if (m.title && state.session) {
    const li = document.querySelector(`#sessions li[data-id="${state.session}"] .t`);
    if (li) li.textContent = m.title;
  }
}

// ------------------------------------------------------------ события
function applyOp(op) {
  if (op.seq && op.seq <= state.seq) return;
  if (op.seq) state.seq = op.seq;
  switch (op.op) {
    case "add":
      if (!state.items.length) emptyNode.remove();
      state.items.push(op.item);
      scheduleRender(state.items.length - 1);
      if (state.items.length > 1) scheduleRender(state.items.length - 2);  // курсор
      break;
    case "append":
      state.items[op.i][op.key] = (state.items[op.i][op.key] || "") + op.text;
      scheduleRender(op.i);
      break;
    case "set":
      Object.assign(state.items[op.i], op.data);
      scheduleRender(op.i);
      if (op.data.diff && !$("#panel").classList.contains("hidden")) loadChanges();
      break;
    case "meta": {
      const wasRunning = state.meta.running;
      state.meta = op.data;
      renderMeta();
      if (wasRunning !== op.data.running) {
        state.items.forEach((it, i) => it.type === "user" && scheduleRender(i));
        if (state.items.length) scheduleRender(state.items.length - 1);
        if (!op.data.running) { loadSessions(); loadFiles(); }
      }
      break;
    }
    case "reset": case "resync":
      openSession(state.session);
      break;
  }
}

function connect() {
  if (state.es) state.es.close();
  const sid = state.session;
  const es = new EventSource(`/api/sessions/${sid}/events?after=${state.seq}`);
  state.es = es;
  es.onmessage = e => { if (state.session === sid) applyOp(JSON.parse(e.data)); };
  es.onerror = () => {
    es.close();
    if (state.es === es) setTimeout(() => state.session === sid && connect(), 1500);
  };
}

// ------------------------------------------------------------- действия
async function openSession(id) {
  if (state.es) { state.es.close(); state.es = null; }
  state.session = id;
  LS.set("session:" + state.project.id, id);
  const s = await api("GET", `/api/sessions/${id}`);
  Object.assign(state, {items: s.items, seq: s.seq, meta: s.meta, rewindFloor: s.rewind_floor});
  renderAll();
  renderMeta();
  document.querySelectorAll("#sessions li").forEach(li => li.classList.toggle("active", li.dataset.id === id));
  connect();
  if (!$("#panel").classList.contains("hidden")) loadChanges();
}

async function newSession() {
  if (!state.project) return;
  const s = await api("POST", "/api/sessions", {project_id: state.project.id,
    settings: {mode: $("#modeSel").value, effort: $("#effortSel").value}});
  await loadSessions();
  await openSession(s.id);
  $("#input").focus();
}

async function send() {
  const box = $("#input");
  if (state.meta.running) { if (state.session) await api("POST", `/api/sessions/${state.session}/stop`); return; }
  const text = box.value.trim();
  if (!text) return;
  if (text === "/clear") { box.value = ""; return newSession(); }
  if (!state.session) await newSession();
  box.value = "";
  try {
    await api("POST", `/api/sessions/${state.session}/message`, {text});
    state.meta.running = true; renderMeta();
  } catch (e) { box.value = text; alert(e.message); }
}

async function approve(id, decision, note) {
  try { await api("POST", `/api/sessions/${state.session}/approve`, {id, decision, note}); }
  catch (e) { alert(e.message); }
}

async function rewindTo(it) {
  if (!confirm("Откатить изменения файлов, сделанные агентом начиная с этого сообщения, и удалить эту часть истории?\n(Изменения от команд run не откатываются.)")) return;
  try {
    const r = await api("POST", `/api/sessions/${state.session}/rewind`, {turn: it.turn});
    $("#input").value = it.text;
    if (r.restored.length) console.info("Восстановлены:", r.restored);
  } catch (e) { alert(e.message); }
}

// ------------------------------------------------------- проекты/файлы
async function loadProjects() {
  state.projects = await api("GET", "/api/projects");
  const sel = $("#projectSel");
  sel.innerHTML = state.projects.map(p => `<option value="${p.id}" title="${esc(p.path)}">${esc(p.name)}</option>`).join("")
    || `<option value="">— добавьте проект —</option>`;
  const saved = LS.get("project");
  const p = state.projects.find(p => p.id === saved) || state.projects[0];
  if (p) { sel.value = p.id; await selectProject(p.id); }
  else $("#projectDlg").showModal();
}

async function selectProject(id) {
  state.project = state.projects.find(p => p.id === id);
  LS.set("project", id);
  document.title = `${state.project.name} — Code Worker`;
  await Promise.all([loadSessions(), loadFiles()]);
  const saved = LS.get("session:" + id);
  const s = state.sessions.find(s => s.id === saved) || state.sessions[0];
  if (s) await openSession(s.id);
  else { state.session = null; state.items = []; state.meta = {}; renderAll(); renderMeta(); }
}

async function loadSessions() {
  if (!state.project) return;
  state.sessions = await api("GET", `/api/projects/${state.project.id}/sessions`);
  $("#sessions").innerHTML = state.sessions.map(s =>
    `<li data-id="${s.id}" class="${s.id === state.session ? "active" : ""}">${s.running ? '<span class="dot"></span>' : ""}<span class="t" title="${esc(s.title)}">${esc(s.title)}</span><span class="x" title="Удалить">✕</span></li>`).join("");
  document.querySelectorAll("#sessions li").forEach(li => {
    li.onclick = e => {
      if (e.target.classList.contains("x")) {
        if (confirm("Удалить сессию?")) api("DELETE", `/api/sessions/${li.dataset.id}`).then(() => {
          if (state.session === li.dataset.id) { state.session = null; state.items = []; renderAll(); }
          loadSessions();
        });
      } else openSession(li.dataset.id);
    };
  });
}

async function loadFiles() {
  if (!state.project) return;
  state.files = await api("GET", `/api/projects/${state.project.id}/files`);
  renderTree();
}

function renderTree() {
  const f = $("#fileFilter").value.trim().toLowerCase();
  const files = f ? state.files.filter(x => x.toLowerCase().includes(f)) : state.files;
  if (f) {
    $("#tree").innerHTML = files.slice(0, 500).map(p => `<div class="f" data-p="${esc(p)}" title="${esc(p)}">${esc(p)}</div>`).join("");
  } else {
    const root = {};
    for (const p of files) {
      let node = root;
      const parts = p.split("/");
      parts.forEach((part, i) => {
        if (i === parts.length - 1) (node["\u0000files"] ||= []).push([part, p]);
        else node = node[part] ||= {};
      });
    }
    const html = node => Object.keys(node).filter(k => k !== "\u0000files").sort().map(k =>
      `<details><summary>${esc(k)}</summary><div>${html(node[k])}</div></details>`).join("")
      + (node["\u0000files"] || []).map(([n, p]) => `<div class="f" data-p="${esc(p)}" title="${esc(p)}">${esc(n)}</div>`).join("");
    $("#tree").innerHTML = html(root);
  }
  document.querySelectorAll("#tree .f").forEach(el => el.onclick = e => {
    if (e.altKey || e.ctrlKey) insertMention(el.dataset.p);
    else openFile(el.dataset.p);
  });
}

async function openFile(path) {
  showPanel("file");
  const r = await api("GET", `/api/projects/${state.project.id}/file?path=${encodeURIComponent(path)}`);
  $("#tab-file").innerHTML = `<div class="change"><div class="ch"><b>${esc(path)}</b><span class="spacer"></span><button class="btn small" id="mentionBtn">@ в сообщение</button></div>`
    + (r.binary ? `<p class="muted">Бинарный файл</p>` : `<div class="code-view">${r.content.split("\n").map(l => `<div>${esc(l) || " "}</div>`).join("")}</div>`) + "</div>";
  $("#mentionBtn").onclick = () => insertMention(path);
}

async function loadChanges() {
  if (!state.session) { $("#tab-changes").innerHTML = `<p class="muted">Нет сессии</p>`; return; }
  const ch = await api("GET", `/api/sessions/${state.session}/changes`);
  const zip = `<p><a class="btn small" href="/api/projects/${state.project.id}/zip">Скачать проект (.zip)</a></p>`;
  $("#tab-changes").innerHTML = zip + (ch.length ? ch.map(c =>
    `<div class="change"><div class="ch"><span class="badge ${c.status}">${c.status === "added" ? "новый" : c.status === "deleted" ? "удалён" : "изменён"}</span><a href="#" data-p="${esc(c.path)}">${esc(c.path)}</a></div>${diffHtml(c.diff)}</div>`).join("")
    : `<p class="muted">Агент ещё не менял файлы в этой сессии.</p>`);
  document.querySelectorAll("#tab-changes a[data-p]").forEach(a => a.onclick = e => { e.preventDefault(); openFile(a.dataset.p); });
}

function showPanel(tab) {
  $("#panel").classList.remove("hidden");
  document.querySelectorAll(".tabs [data-tab]").forEach(b => b.classList.toggle("active", b.dataset.tab === tab));
  $("#tab-changes").classList.toggle("hidden", tab !== "changes");
  $("#tab-file").classList.toggle("hidden", tab !== "file");
  if (tab === "changes") loadChanges();
}

// ---------------------------------------------------------- @-упоминания
let mention = {active: false, sel: 0, list: []};
function insertMention(path) {
  const box = $("#input");
  box.value += (box.value && !box.value.endsWith(" ") ? " " : "") + "@" + path + " ";
  box.focus();
}
function updateMention() {
  const box = $("#input");
  const before = box.value.slice(0, box.selectionStart);
  const m = before.match(/(?:^|\s)@([\w./\\-]*)$/);
  if (!m) { mention.active = false; $("#mentionBox").classList.add("hidden"); return; }
  const q = m[1].toLowerCase();
  mention.list = state.files.filter(f => f.toLowerCase().includes(q)).slice(0, 30);
  mention.active = mention.list.length > 0;
  mention.sel = 0;
  mention.q = m[1];
  drawMention();
}
function drawMention() {
  const b = $("#mentionBox");
  b.classList.toggle("hidden", !mention.active);
  b.innerHTML = mention.list.map((f, i) => `<div class="${i === mention.sel ? "sel" : ""}" data-i="${i}">${esc(f)}</div>`).join("");
  b.querySelectorAll("div").forEach(d => d.onmousedown = e => { e.preventDefault(); pickMention(+d.dataset.i); });
}
function pickMention(i) {
  const box = $("#input"), pos = box.selectionStart;
  const start = pos - mention.q.length;
  box.value = box.value.slice(0, start) + mention.list[i] + " " + box.value.slice(pos);
  box.selectionStart = box.selectionEnd = start + mention.list[i].length + 1;
  mention.active = false;
  $("#mentionBox").classList.add("hidden");
}

// --------------------------------------------------------------- модель
async function pollModel() {
  try {
    const st = await api("GET", "/api/status");
    state.context = st.context;
    const pill = $("#modelPill");
    pill.onclick = null;
    pill.classList.remove("clickable");
    if (!st.ok) { pill.className = "pill off"; pill.textContent = "AI Manager недоступен"; pill.title = st.error; }
    else if (st.switching) {
      pill.className = "pill wait";
      pill.textContent = "AI Manager: загрузка модели…";
      pill.title = "AI Manager отвечает медленно — обычно идёт запуск или переключение модели (30–60 с).";
    }
    else if (st.ready) { pill.className = "pill ok"; pill.textContent = st.model; pill.title = "Модель запущена"; }
    else if (st.busy) {
      // По другой модели есть задачи — AI Manager не переключит GPU, наш запрос подождёт в очереди
      pill.className = "pill wait";
      pill.textContent = `${st.model}: GPU занята (${st.active})`;
      pill.title = `Выполняется задач: ${st.queue_running}, ждут: ${st.queue_waiting}. ` +
                   "Запрос встанет в очередь, модель запустится после них.";
    }
    else {
      pill.className = "pill wait clickable";
      pill.textContent = `${st.model}: не запущена`;
      pill.title = st.active ? `Активна ${st.active}. Нажмите, чтобы переключить (или модель запустится сама при первом запросе)` : "Нажмите, чтобы запустить (или запустится сама при первом запросе)";
      pill.onclick = async () => {
        pill.textContent = "запуск…"; pill.onclick = null;
        try { await api("POST", "/api/model/start"); } catch (e) { alert(e.message); }
        pollModel();
      };
    }
  } catch {}
}

// ---------------------------------------------------------------- init
function bind() {
  $("#sendBtn").onclick = send;
  $("#newSession").onclick = newSession;
  $("#refreshFiles").onclick = loadFiles;
  $("#fileFilter").oninput = renderTree;
  $("#projectSel").onchange = e => selectProject(e.target.value);
  $("#addProject").onclick = () => { $("#projectErr").textContent = ""; $("#projectDlg").showModal(); };
  $("#toggleSide").onclick = () => $("#side").classList.toggle("collapsed");
  $("#togglePanel").onclick = () => $("#panel").classList.contains("hidden") ? showPanel("changes") : $("#panel").classList.add("hidden");
  $("#panelRefresh").onclick = () => $("#tab-changes").classList.contains("hidden") ? null : loadChanges();
  document.querySelectorAll(".tabs [data-tab]").forEach(b => b.onclick = () => showPanel(b.dataset.tab));
  for (const [sel, key] of [["#modeSel", "mode"], ["#effortSel", "effort"]]) {
    $(sel).onchange = e => {
      LS.set(key, e.target.value);
      if (state.session) api("PATCH", `/api/sessions/${state.session}`, {settings: {[key]: e.target.value}});
    };
    $(sel).value = LS.get(key, $(sel).value);
  }
  const box = $("#input");
  box.addEventListener("keydown", e => {
    if (mention.active) {
      if (e.key === "ArrowDown") { mention.sel = (mention.sel + 1) % mention.list.length; drawMention(); e.preventDefault(); return; }
      if (e.key === "ArrowUp") { mention.sel = (mention.sel - 1 + mention.list.length) % mention.list.length; drawMention(); e.preventDefault(); return; }
      if (e.key === "Enter" || e.key === "Tab") { pickMention(mention.sel); e.preventDefault(); return; }
      if (e.key === "Escape") { mention.active = false; drawMention(); return; }
    }
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
  });
  box.addEventListener("input", updateMention);
  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && state.meta.running && !mention.active) api("POST", `/api/sessions/${state.session}/stop`);
  });
  $("#projectForm").addEventListener("submit", async e => {
    if (e.submitter?.value === "cancel") return;
    e.preventDefault();
    const f = e.target, btn = $("#projectOk");
    btn.disabled = true; btn.textContent = "Добавление…";
    try {
      const p = await api("POST", "/api/projects", {path: f.path.value, git_url: f.git_url.value, branch: f.branch.value});
      $("#projectDlg").close();
      f.reset();
      LS.set("project", p.id);
      await loadProjects();
    } catch (err) { $("#projectErr").textContent = err.message; }
    finally { btn.disabled = false; btn.textContent = "Добавить"; }
  });
  if (innerWidth <= 800) $("#side").classList.add("collapsed");
}

async function init() {
  await pollModel();
  await loadProjects();
}

bind();
init();
setInterval(pollModel, 15000);
