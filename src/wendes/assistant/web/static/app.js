// Página da reunião do Wendes (modo assist). JS puro, sem etapa de build.
// Modos: "live" / "live-edit" (servidor com SSE) e "export" (estado embutido).
const MODE = document.body.dataset.mode || "live";
const LIVE = MODE.startsWith("live");
const CAN_EDIT = MODE === "live-edit";

const S = {
  segments: [], notes: {}, qa: [], artifacts: [], prev_actions: [],
  status: {}, run_id: "", room_url: "", date: "", started_at: "",
  filter: "", activeArt: null, versionSel: {}, editing: null, reported: new Set(),
};

const $ = (id) => document.getElementById(id);
const base = () => location.pathname.replace(/\/$/, "");

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function mmss(t) {
  t = Math.max(0, Math.floor(Number(t) || 0));
  const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
  const p = (n) => String(n).padStart(2, "0");
  return h ? `${h}:${p(m)}:${p(s)}` : `${p(m)}:${p(s)}`;
}

function parseTs(txt) {
  const parts = txt.split(":").map(Number);
  return parts.reduce((acc, n) => acc * 60 + n, 0);
}

// [mm:ss] vira link para o segmento (só dígitos: seguro depois da sanitização)
function linkTimestamps(html) {
  return html.replace(/\[(\d{1,2}:\d{2}(?::\d{2})?)\]/g,
    (_, ts) => `<a href="#" class="ts" data-ts="${parseTs(ts)}">[${ts}]</a>`);
}

function md(text) {
  const src = String(text ?? "");
  if (window.marked && window.DOMPurify) {
    return linkTimestamps(window.DOMPurify.sanitize(window.marked.parse(src)));
  }
  return linkTimestamps(`<pre>${esc(src)}</pre>`);
}

function color(name) {
  let h = 0;
  for (const c of name) h = (h * 31 + c.charCodeAt(0)) % 360;
  return `hsl(${h} 65% 45%)`;
}

const storage = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* sem storage */ } },
};

// ---- cabeçalho ---------------------------------------------------------------
function renderHeader() {
  const st = S.status;
  $("h-room").textContent = S.room_url ? S.room_url.replace(/^https?:\/\//, "") : "Wendes";
  $("h-start").textContent = S.date ? `${S.date} · início ${S.started_at}` : "";
  const status = $("h-status");
  const map = {
    recording: ["● gravando", "rec"], paused: ["⏸ pausado", "paused"], finished: ["■ encerrada", ""],
  };
  const [label, cls] = map[st.status] || ["…", ""];
  status.textContent = st.status === "paused" && st.paused_by ? `${label} (${st.paused_by})` : label;
  status.className = `badge ${cls}`;
  $("h-duration").textContent = mmss(st.position || 0);
  const lag = Number(st.stt_lag || 0);
  $("h-lag").textContent = `STT ${Math.round(lag)} s`;
  $("h-lag").className = `badge ${lag > 15 ? "warn" : ""}`;
  $("h-llm").textContent = `LLM ${st.llm_queue || 0}`;
  $("h-llm").hidden = !LIVE;
  $("h-lag").hidden = !LIVE;
  const ps = st.participants || [];
  $("h-participants").textContent = ps.length ? `Participantes: ${ps.join(", ")}` : "";
}

// ---- transcrição -------------------------------------------------------------
function segLi(seg) {
  const li = document.createElement("li");
  li.id = `seg-${seg.id}`;
  li.dataset.start = seg.start;
  if (seg.marker) li.className = "marker";
  if (seg.source === "bot") li.classList.add("bot");
  const who = seg.marker ? "" : (seg.speaker || "não identificado");
  const whoHtml = who
    ? `<span class="who" style="color:${seg.speaker ? color(seg.speaker) : "inherit"}"
        title="${seg.speaker_conf ? `confiança ${Math.round(seg.speaker_conf * 100)}%` : ""}">${esc(who)}:</span>`
    : "";
  li.innerHTML = `<a href="#" class="ts" data-ts="${seg.start}">${mmss(seg.start)}</a>${whoHtml}${esc(seg.text)}`;
  return li;
}

function matches(seg) {
  if (!S.filter) return true;
  return `${seg.speaker} ${seg.text}`.toLowerCase().includes(S.filter);
}

function renderTranscript() {
  const ol = $("t-list");
  ol.replaceChildren(...S.segments.filter(matches).map(segLi));
  scrollTranscript();
}

function appendSegment(seg) {
  S.segments.push(seg);
  if (matches(seg)) $("t-list").appendChild(segLi(seg));
  scrollTranscript();
}

function scrollTranscript() {
  if (!$("t-autoscroll").checked) return;
  const ol = $("t-list");
  const last = ol.lastElementChild;
  if (last) last.scrollIntoView({ block: "end" });
}

function jumpTo(ts) {
  let best = null;
  for (const seg of S.segments) {
    if (seg.start <= ts + 0.5) best = seg; else break;
  }
  if (!best) return;
  if (S.filter) { S.filter = ""; $("t-search").value = ""; renderTranscript(); }
  showTab("transcricao");
  $("t-autoscroll").checked = false;
  const li = $(`seg-${best.id}`);
  if (!li) return;
  document.querySelectorAll("li.hl").forEach((el) => el.classList.remove("hl"));
  li.classList.add("hl");
  li.scrollIntoView({ block: "center" });
}

// ---- notas e ações -----------------------------------------------------------
const tsLink = (ts) => (ts || ts === 0) && ts !== "" ? ` <a href="#" class="ts" data-ts="${ts}">[${mmss(ts)}]</a>` : "";

function renderNotes() {
  const n = S.notes || {};
  const topics = n.topicos || [];
  const parts = [];
  if (topics.length) {
    for (const t of topics) {
      parts.push(`<h3>${esc(t.titulo)}</h3><ul>${(t.pontos || [])
        .map((p) => `<li>${esc(p.texto)}${tsLink(p.ts)}</li>`).join("")}</ul>`);
    }
  }
  if ((n.decisoes || []).length) {
    parts.push(`<h2>Decisões</h2><ul>${n.decisoes
      .map((d) => `<li>${esc(d.texto)}${tsLink(d.ts)}</li>`).join("")}</ul>`);
  }
  if ((n.perguntas_abertas || []).length) {
    parts.push(`<h2>Perguntas em aberto</h2><ul>${n.perguntas_abertas
      .map((q) => `<li>${esc(q.texto)}${tsLink(q.ts)}</li>`).join("")}</ul>`);
  }
  const body = $("n-body");
  body.className = parts.length ? "" : "muted";
  body.innerHTML = parts.join("") || "Ainda sem notas.";
}

function renderActions() {
  const acoes = (S.notes && S.notes.acoes) || [];
  const a = $("a-body");
  if (acoes.length) {
    a.className = "";
    a.innerHTML = `<table><thead><tr><th>#</th><th>Descrição</th><th>Responsável</th>
      <th>Prazo</th><th>Status</th></tr></thead><tbody>${acoes.map((x) => `<tr>
      <td>${esc(x.id)}</td><td>${esc(x.descricao)}${tsLink(x.ts)}</td>
      <td>${esc(x.responsavel || "—")}</td><td>${esc(x.prazo || "—")}</td>
      <td>${esc(x.status)}</td></tr>`).join("")}</tbody></table>`;
  } else {
    a.className = "muted";
    a.textContent = "Nenhuma ação registrada.";
  }
  const p = $("p-body");
  const prev = S.prev_actions || [];
  if (prev.length) {
    const tag = (s) => s === "resolvida" ? "ok" : s === "mencionada" ? "mention" : "";
    p.className = "";
    p.innerHTML = `<table><thead><tr><th>#</th><th>Descrição</th><th>Responsável</th>
      <th>De</th><th>Nesta reunião</th></tr></thead><tbody>${prev.map((x) => `<tr>
      <td>${esc(x.ref)}</td><td>${esc(x.descricao)}</td><td>${esc(x.responsavel || "—")}</td>
      <td>${esc(x.meeting_date || "")}</td>
      <td><span class="tag ${tag(x.situacao)}">${esc(x.situacao || "sem menção")}</span>
      ${tsLink(x.evidencia_ts)}</td></tr>`).join("")}</tbody></table>`;
  } else {
    p.className = "muted";
    p.textContent = "Nenhuma pendência anterior.";
  }
}

// ---- perguntas ---------------------------------------------------------------
function renderQA() {
  const body = $("q-body");
  if (!S.qa.length) { body.className = "muted"; return; }
  body.className = "";
  body.innerHTML = S.qa.slice().reverse().map((q) => {
    const cites = (q.citacoes || []).map((c) => c.data
      ? `<span class="tag">${esc(c.data)} ${mmss(c.ts || 0)}</span>`
      : `<a href="#" class="ts" data-ts="${Number(c.ts) || 0}">[${mmss(c.ts)}]</a>`).join(" ");
    return `<div class="qa"><div class="q">${esc(q.pergunta)}</div>
      <div class="meta">${esc(q.quem || "")} · ${esc(q.via || "")}${q.privada ? " · privada" : ""}
      · ${mmss(q.ts || 0)}</div>
      <div class="a">${md(q.resposta || "")}</div>
      ${cites ? `<div class="meta">Citações: ${cites}</div>` : ""}</div>`;
  }).join("");
}

// ---- artefatos ---------------------------------------------------------------
let mermaidReady = false;
function initMermaid() {
  if (!window.mermaid || mermaidReady) return;
  const dark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  window.mermaid.initialize({ startOnLoad: false, securityLevel: "strict", theme: dark ? "dark" : "default" });
  mermaidReady = true;
}

let renderSeq = 0;
async function renderMermaid(el, code, art, ver) {
  initMermaid();
  if (!window.mermaid) {
    el.innerHTML = `<pre>${esc(code)}</pre><div class="notice">Mermaid indisponível: mostrando o código.</div>`;
    return;
  }
  try {
    const { svg } = await window.mermaid.render(`mmd-${art.id}-${ver.n}-${++renderSeq}`, code);
    el.innerHTML = svg;
  } catch (err) {
    el.innerHTML = `<pre>${esc(code)}</pre><div class="notice">Não consegui desenhar este diagrama: mostrando o código.</div>`;
    const key = `${art.id}:${ver.n}`;
    if (LIVE && !S.reported.has(key)) {
      S.reported.add(key);
      fetch(`${base()}/artifacts/${art.id}/render-error`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ versao: ver.n, erro: String(err && err.message || err) }),
      }).catch(() => {});
    }
  }
}

function currentVersion(art) {
  const vs = art.versoes || [];
  const sel = S.versionSel[art.id];
  return vs.find((v) => v.n === sel) || vs[vs.length - 1];
}

function renderArtifacts() {
  const tabs = $("art-tabs");
  const body = $("art-body");
  const arts = S.artifacts || [];
  if (!arts.length) { tabs.replaceChildren(); return; }
  if (!arts.some((a) => a.id === S.activeArt)) S.activeArt = arts[arts.length - 1].id;
  tabs.replaceChildren(...arts.map((a) => {
    const b = document.createElement("button");
    b.textContent = `#${a.id} ${a.titulo}`;
    b.className = a.id === S.activeArt ? "active" : "";
    b.onclick = () => { S.activeArt = a.id; S.editing = null; renderArtifacts(); };
    return b;
  }));
  const art = arts.find((a) => a.id === S.activeArt);
  const ver = currentVersion(art);
  if (!ver) return;
  body.className = "";
  const ext = art.formato === "mmd" ? "mmd" : "md";
  const options = (art.versoes || []).map((v) =>
    `<option value="${v.n}" ${v.n === ver.n ? "selected" : ""}>v${v.n} · ${esc(v.autor || "")}</option>`).join("");
  body.innerHTML = `<div class="art-tools">
      <strong>${esc(art.titulo)}</strong> <span class="tag">${esc(art.tipo)}</span>
      <select id="art-ver" aria-label="versão">${options}</select>
      <button id="art-dl">baixar .${ext}</button>
      <button id="art-copy">copiar</button>
      ${CAN_EDIT ? `<button id="art-edit">editar</button>` : ""}
    </div>
    <div class="small muted">${ver.pedido ? `Pedido: ${esc(ver.pedido)}` : ""}</div>
    <div id="art-content" class="art-content"></div>`;
  $("art-ver").onchange = (e) => { S.versionSel[art.id] = Number(e.target.value); renderArtifacts(); };
  $("art-dl").onclick = () => {
    const blob = new Blob([ver.conteudo || ""], { type: "text/plain;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${art.slug || `artefato-${art.id}`}-v${ver.n}.${ext}`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  };
  $("art-copy").onclick = async () => {
    try { await navigator.clipboard.writeText(ver.conteudo || ""); $("art-copy").textContent = "copiado"; }
    catch { $("art-copy").textContent = "não foi possível copiar"; }
  };
  const content = $("art-content");
  if (S.editing === art.id) {
    content.innerHTML = `<div class="art-edit">
      <textarea id="art-text"></textarea>
      <div class="art-tools"><label class="small">Seu nome <input id="art-author"></label>
      <button id="art-save">salvar nova versão</button><button id="art-cancel">cancelar</button>
      <span id="art-msg" class="small"></span></div></div>`;
    $("art-text").value = ver.conteudo || "";
    $("art-author").value = storage.get("wendes.author") || "";
    $("art-cancel").onclick = () => { S.editing = null; renderArtifacts(); };
    $("art-save").onclick = async () => {
      const autor = $("art-author").value.trim();
      storage.set("wendes.author", autor);
      try {
        const r = await fetch(`${base()}/artifacts/${art.id}`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ conteudo: $("art-text").value, autor }),
        });
        if (!r.ok) throw new Error((await r.json()).erro || r.status);
        S.editing = null;
        S.versionSel[art.id] = undefined;
      } catch (err) {
        $("art-msg").textContent = `Falhou: ${err.message || err}`;
      }
    };
    return;
  }
  if (CAN_EDIT) $("art-edit").onclick = () => { S.editing = art.id; renderArtifacts(); };
  if (art.formato === "mmd") renderMermaid(content, ver.conteudo || "", art, ver);
  else content.innerHTML = md(ver.conteudo || "");
}

// ---- abas e eventos ------------------------------------------------------------
function showTab(name) {
  document.querySelectorAll("nav#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("active", p.id === name));
}

function renderAll() {
  renderHeader(); renderTranscript(); renderNotes(); renderActions(); renderQA();
  if (S.editing === null) renderArtifacts();
}

function load(snap) {
  Object.assign(S, {
    segments: snap.segments || [], notes: snap.notes || {}, qa: snap.qa || [],
    artifacts: snap.artifacts || [], prev_actions: snap.prev_actions || [],
    run_id: snap.run_id || "", room_url: snap.room_url || "", date: snap.date || "",
    started_at: snap.started_at || "",
    status: {
      status: snap.status, paused_by: snap.paused_by, stt_lag: snap.stt_lag,
      llm_queue: snap.llm_queue, position: snap.position, participants: snap.participants,
    },
  });
  if (!LIVE) S.status.status = "finished";
  renderAll();
}

document.addEventListener("click", (e) => {
  const a = e.target.closest("a.ts");
  if (a) { e.preventDefault(); jumpTo(Number(a.dataset.ts)); }
  const tab = e.target.closest("nav#tabs button");
  if (tab) showTab(tab.dataset.tab);
});
$("t-search").addEventListener("input", (e) => { S.filter = e.target.value.trim().toLowerCase(); renderTranscript(); });

async function fetchState() {
  const r = await fetch(`${base()}/state`, { credentials: "same-origin" });
  if (!r.ok) throw new Error(`estado: ${r.status}`);
  load(await r.json());
}

function connect() {
  const es = new EventSource(`${base()}/events`);
  let broken = false;
  es.addEventListener("segment", (e) => appendSegment(JSON.parse(e.data)));
  es.addEventListener("status", (e) => { S.status = JSON.parse(e.data); renderHeader(); });
  es.addEventListener("notes", (e) => { S.notes = JSON.parse(e.data); renderNotes(); renderActions(); });
  es.addEventListener("prev_actions", (e) => { S.prev_actions = JSON.parse(e.data); renderActions(); });
  es.addEventListener("qa", (e) => { S.qa.push(JSON.parse(e.data)); renderQA(); });
  es.addEventListener("artifacts", (e) => {
    S.artifacts = JSON.parse(e.data);
    if (S.editing === null) renderArtifacts();
  });
  es.onerror = () => { broken = true; };
  es.onopen = () => { if (broken) { broken = false; fetchState().catch(() => {}); } };
}

if (LIVE) {
  fetchState().then(connect).catch((err) => {
    $("t-list").innerHTML = `<li class="notice">Falha ao carregar: ${esc(err.message)}</li>`;
  });
} else {
  load(window.WENDES_STATE || {});
}
