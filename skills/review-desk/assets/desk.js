// Review Desk client. State lives on disk behind the server; this page renders it and posts
// the user's half of the protocol (messages, flags, suggested edits, execute, end).
"use strict";

const D = window.DESK;
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const MODEL_LABEL = { haiku: "Haiku", sonnet: "Sonnet", opus: "Opus", fable: "Fable" };
const EXT_LANG = {
  py: "python", rs: "rust", ts: "typescript", tsx: "typescript", js: "javascript", mjs: "javascript", cjs: "javascript",
  jsx: "javascript", go: "go", java: "java", kt: "kotlin", rb: "ruby", sh: "bash", zsh: "bash", c: "c", h: "c", cc: "cpp",
  cpp: "cpp", hpp: "cpp", cs: "csharp", swift: "swift", sql: "sql", json: "json", yaml: "yaml", yml: "yaml", toml: "ini",
  md: "markdown", html: "xml", css: "css", scss: "scss", php: "php", lua: "lua",
};
const MAX_HIGHLIGHT_ROWS = 6000;

const S = {
  state: null, manifest: { files: [] }, byPath: new Map(), diffs: new Map(), files: new Map(),
  tabs: ["story"], active: "story", view: "diff", layout: "unified", sel: null, dragging: false,
  anchors: [], tier: null, tierTouched: false, chatSig: "", backlogSig: "", reloaded: undefined,
  trayKnown: null, picked: new Set(), pane: "chat", filter: "", closedDirs: new Set(), edit: null,
  live: null, selSource: null, skills: [], slashIdx: 0, waitTimer: null, draft: null,
  pageSeen: new Map(),
  // the commit picker: S.all is the review's whole change set, S.manifest the entry on screen
  all: { files: [] }, commits: { entries: [], uncommitted: null }, commit: "all", views: new Map(),
};
// Tabs are "story", a repository path, or "page:P<n>" (an HTML page shown read-only).
const isPage = (t) => typeof t === "string" && t.startsWith("page:");
const isCode = (t) => t !== "story" && !isPage(t);
const pageOf = (t) => (S.state && S.state.pages || []).find((pg) => `page:${pg.id}` === t);
const pageName = (pg) => pg.title || pg.path.split("/").pop();
// A commit picker entry: "all" (the whole review), a commit's sha, or "uncommitted" (the working tree vs HEAD).
// Only a commit's own view shows lines that may differ from the working tree's.
const isSha = (c) => typeof c === "string" && /^[0-9a-f]{40}(?:[0-9a-f]{24})?$/.test(c);

// ------------------------------------------------------------------ api
async function api(path, opts = {}) {
  const r = await fetch(`/api/${D.sid}/${path}`, {
    ...opts, headers: { "X-Desk-Token": D.token, "Content-Type": "application/json", ...(opts.headers || {}) },
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
  return j;
}
const post = (path, body) => api(path, { method: "POST", body: JSON.stringify(body) });

function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (err ? " err" : "");
  t.hidden = false;
  clearTimeout(toast.h);
  toast.h = setTimeout(() => { t.hidden = true; }, err ? 5000 : 2600);
}

const tierLabel = (t) => t && t.model ? `${MODEL_LABEL[t.model] || t.model} · ${t.effort}` : "";
const langFor = (path) => {
  const name = path.split("/").pop().toLowerCase();
  if (name === "dockerfile" || name.startsWith("dockerfile.")) return "dockerfile";
  return EXT_LANG[name.includes(".") ? name.split(".").pop() : ""] || null;
};

// ------------------------------------------------------------ highlight
// hljs output split into one well-formed HTML string per line (spans reopened across newlines).
function splitLines(html) {
  const lines = [], open = [];
  let cur = "", m;
  const re = /<span[^>]*>|<\/span>|[^<]+/g;
  while ((m = re.exec(html))) {
    const t = m[0];
    if (t.startsWith("<span")) { open.push(t); cur += t; }
    else if (t === "</span>") { open.pop(); cur += t; }
    else t.split("\n").forEach((part, i) => {
      if (i > 0) { lines.push(cur + "</span>".repeat(open.length)); cur = open.join(""); }
      cur += part;
    });
  }
  lines.push(cur + "</span>".repeat(open.length));
  return lines;
}

// Paint text cells: segments break at hunks that hide lines, the old side (context + removed)
// and the new side (context + added) are highlighted separately, like the summary page does.
function paint(cells, lang) {
  if (!window.hljs || !lang || !hljs.getLanguage(lang) || cells.length > MAX_HIGHLIGHT_ROWS) return;
  const run = (list, owns) => {
    if (!list.length) return;
    const html = hljs.highlight(list.map((c) => c.text).join("\n"), { language: lang, ignoreIllegals: true }).value;
    splitLines(html).forEach((line, i) => { if (list[i] && owns(list[i])) list[i].el.innerHTML = line || "​"; });
  };
  let seg = [];
  const flush = () => {
    if (seg.some((c) => c.cls === "del")) run(seg.filter((c) => c.cls !== "add"), (c) => c.cls === "del");
    run(seg.filter((c) => c.cls !== "del"), () => true);
    seg = [];
  };
  cells.forEach((c) => {
    if (c.cls === "meta" || (c.cls.startsWith("hunk") && !c.cls.includes("join"))) flush();
    else if (!c.cls.startsWith("hunk")) seg.push(c);
  });
  flush();
}

// ------------------------------------------------------------- markdown
function inline(s) {
  const codes = [];
  s = s.replace(/`([^`]+)`/g, (_, c) => { codes.push(c); return `\u0000${codes.length - 1}\u0000`; });
  s = esc(s)
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
    .replace(/(^|[\s(])(https?:\/\/[^\s<)]+[^\s<).,;:])/g, '$1<a href="$2" target="_blank" rel="noopener" class="url">$2</a>')
    .replace(/(^|[\s(])((?:[\w.-]+\/)*[\w.-]+\.\w+:\d+(?:-\d+)?)(?=[\s).,;:]|$)/g, (m, pre, ref) => `${pre}${locLink(ref)}`)
    .replace(/(^|[\s(])((?:~\/|\/)[\w./@+-]+\.html?)(?=[\s).,;:]|$)/g, (m, pre, ref) => `${pre}${pageLink(ref)}`);
  // highlights in plain text only: never inside a tag, a link or (placeholdered) code
  s = s.split(/(<a\b[^>]*>[\s\S]*?<\/a>|<[^>]+>)/).map((part) => part.startsWith("<") ? part : part
    .replace(/\bB(\d+)\b/g, '<span class="bid">B$1</span>')
    .replace(/(^|[\s(])#(\d+)\b/g, '$1<span class="ref">#$2</span>')).join("");
  return s.replace(/\u0000(\d+)\u0000/g, (_, i) => {
    const c = codes[+i];
    if (/^(?:[\w.-]+\/)*[\w.-]+\.\w+:\d+(?:-\d+)?$/.test(c)) return locLink(c);
    return PAGE_RE.test(c) && !/\s/.test(c) ? pageLink(c) : `<code>${esc(c)}</code>`;
  });
}

// Nested lists from indentation (2 spaces per level, at most 3 levels), so the chat can grey deeper levels.
function renderList(items) {
  let html = "";
  const stack = [];
  items.forEach((it) => {
    const depth = Math.min(it.level + 1, stack.length + 1);
    if (!stack.length || depth > stack.length) { stack.push(it.tag); html += `<${it.tag}>`; }
    else {
      while (stack.length > depth) html += `</li></${stack.pop()}>`;
      html += "</li>";
    }
    html += `<li>${inline(it.text)}`;
  });
  while (stack.length) html += `</li></${stack.pop()}>`;
  return html;
}
const locLink = (ref) => `<a class="loc" data-ref="${esc(ref)}">${esc(ref)}</a>`;
// An HTML file named in the chat opens as a page tab.
const PAGE_RE = /^(?:~\/|\/|(?:[\w.-]+\/)*)[\w./@+-]*\.html?$/;
const pageLink = (ref) => `<a class="loc page-link" data-page-path="${esc(ref)}" title="Open as a page tab">${esc(ref)}</a>`;

// Code blocks: path:line refs (a call tree's trailing comments) become desk links, and, like the
// analyze-code figures, a name followed by "(" reads as a function and a Capitalized name as a type,
// which highlight.js leaves plain. Text inside strings and comments only gets the links.
const REF_RE = /(^|[\s(])((?:[\w.-]+\/)*[\w.-]+\.\w+:\d+(?:-\d+)?)(?=[\s).,;:]|$)/g;
function enrichCode(html, highlighted) {
  const open = [];
  return html.split(/(<[^>]+>)/).map((part) => {
    if (part.startsWith("</")) { open.pop(); return part; }
    if (part.startsWith("<")) { open.push(part); return part; }
    const quoted = open.some((t) => /hljs-(comment|string|doctag|meta)/.test(t));
    let out = part.replace(REF_RE, (m, pre, ref) => `${pre}\u0001${ref}\u0002`);
    if (highlighted && !quoted) {
      out = out.replace(/\b([A-Za-z_]\w*)(?=\s*\()/g, '<span class="hljs-title function_">$1</span>')
        .replace(/(^|[^\w>])([A-Z][a-z]\w*)\b(?![^<]*<\/span>)/g, '$1<span class="hljs-type">$2</span>');
    }
    return out.replace(/\u0001([^\u0002]+)\u0002/g, (m, ref) => locLink(ref));
  }).join("");
}

function md(text) {
  const out = [];
  let open = 0;  // <details> blocks still open: they may wrap a code fence, so they span parts
  const parts = String(text || "").split(/^```(\w*)[^\n]*\n([\s\S]*?)^```[ \t]*$/m);
  for (let i = 0; i < parts.length; i += 3) {
    const prose = parts[i];
    let list = null, para = [];
    const endPara = () => { if (para.length) { out.push(`<p>${para.map(inline).join("<br>")}</p>`); para = []; } };
    const endList = () => { if (list) { out.push(renderList(list.items)); list = null; } };
    prose.split("\n").forEach((line) => {
      const ul = line.match(/^(\s*)[-*]\s+(.*)$/), ol = line.match(/^(\s*)\d+[.)]\s+(.*)$/);
      if (ul || ol) {
        endPara();
        const m = ul || ol;
        const level = Math.min(2, Math.floor(m[1].replace(/\t/g, "  ").length / 2));
        if (!list || (level === 0 && list.items[0].tag !== (ul ? "ul" : "ol"))) { endList(); list = { items: [] }; }
        list.items.push({ level, tag: ul ? "ul" : "ol", text: m[2] });
      } else if (!line.trim()) { endPara(); endList(); }
      else if (/^#{1,4}\s/.test(line)) { endPara(); endList(); out.push(`<p><strong>${inline(line.replace(/^#+\s*/, ""))}</strong></p>`); }
      // collapsible blocks, as answers wrap code snippets: <details> / <summary>…</summary> / </details> lines
      else if (/^\s*<details>\s*$/i.test(line)) { endPara(); endList(); out.push('<details class="md-details">'); open++; }
      else if (open && /^\s*<summary>(.*)<\/summary>\s*$/i.test(line)) { endPara(); endList(); out.push(`<summary>${inline(line.trim().slice(9, -10))}</summary>`); }
      else if (/^\s*<\/details>\s*$/i.test(line)) { endPara(); endList(); if (open) { out.push("</details>"); open--; } }
      else { endList(); para.push(line); }
    });
    endPara(); endList();
    if (i + 2 < parts.length) {
      const lang = parts[i + 1], code = parts[i + 2].replace(/\n$/, "");
      let html = esc(code);
      if (window.hljs && lang && hljs.getLanguage(lang)) html = hljs.highlight(code, { language: lang, ignoreIllegals: true }).value;
      html = enrichCode(html, !!(lang && window.hljs && hljs.getLanguage(lang)));
      out.push(`<pre><code>${html}</code></pre>`);
    }
  }
  return out.join("") + "</details>".repeat(open);
}

// ------------------------------------------------------------------ tree
function renderTree() {
  const files = S.manifest.files || [];
  $("#file-count").textContent = files.length;
  const f = S.filter.toLowerCase();
  const groups = new Map();
  files.filter((x) => !f || x.path.toLowerCase().includes(f)).forEach((x) => {
    const dir = x.path.includes("/") ? x.path.slice(0, x.path.lastIndexOf("/") + 1) : "./";
    if (!groups.has(dir)) groups.set(dir, []);
    groups.get(dir).push(x);
  });
  const html = [];
  for (const [dir, list] of groups) {
    const closed = S.closedDirs.has(dir) && !f;
    html.push(`<div class="dir${closed ? " closed" : ""}" data-dir="${esc(dir)}"><div class="dir-head"><span class="caret">▾</span><span class="name" title="${esc(dir)}">${esc(dir)}</span><span>${list.length}</span></div><div class="dir-files">`);
    list.forEach((x) => {
      const name = x.path.slice(dir === "./" ? 0 : dir.length);
      const stat = x.binary ? "bin" : `<span class="a">+${x.adds}</span> <span class="d">−${x.dels}</span>`;
      html.push(`<div class="file${S.active === x.path ? " active" : ""}" draggable="true" data-path="${esc(x.path)}" title="${esc(x.old_path ? x.old_path + " → " + x.path : x.path)}">` +
        `<span class="st" data-s="${esc(x.badge)}">${esc(x.badge)}</span><span class="fname">${esc(name)}</span>` +
        `${x.cited ? '<span class="story-dot" title="cited in the summary"></span>' : ""}<span class="stat">${stat}</span></div>`);
    });
    html.push("</div></div>");
  }
  $("#tree").innerHTML = html.join("") || '<p class="empty">No changed files.</p>';
}

// ---------------------------------------------------------- commit picker
// "All changes", then each commit since the review's base, oldest first, then the uncommitted rest.
// A reload appends the commits made since; the picked entry scopes the tree, the diffs and the file view.
const COMMIT_KEY = `review-desk:${D.sid}:commit`;
function viewIds() {
  return ["all", ...S.commits.entries.map((e) => e.id), ...(S.commits.uncommitted ? ["uncommitted"] : [])];
}
function rememberedCommit() { try { return sessionStorage.getItem(COMMIT_KEY) || "all"; } catch (_) { return "all"; } }
async function viewManifest(id) {
  if (id === "all") return S.all;
  if (!S.views.has(id)) S.views.set(id, api(`manifest?c=${id}`).catch(() => ({ files: [] })));
  return S.views.get(id);
}
async function setCommit(id, opts = {}) {
  if (!viewIds().includes(id)) id = "all";
  const moved = id !== S.commit;
  S.commit = id;
  const m = await viewManifest(id);
  if (S.commit !== id) return;  // a later pick won
  S.manifest = m;
  S.byPath = new Map((m.files || []).map((f) => [f.path, f]));
  try { sessionStorage.setItem(COMMIT_KEY, id); } catch (_) {}
  if (moved) { setSel(null); closePop(); }
  renderTree();
  renderPicker();
  if (moved && opts.why) toast(opts.why);
  if (moved && !opts.quiet && isCode(S.active)) {
    if (S.byPath.has(S.active)) S.view = "diff";
    renderCode();
  }
}
function stepCommit(by) {
  const ids = viewIds(), i = ids.indexOf(S.commit) + by;
  if (i >= 0 && i < ids.length) setCommit(ids[i]);
}
function entryOf(id) {
  if (id === "all") {
    const f = S.all.files || [], n = S.commits.entries.length;
    return { id, sha: "", title: "All changes", sub: `vs ${S.all.base_label || "base"} · ${n} commit${n === 1 ? "" : "s"}${S.commits.uncommitted ? " + uncommitted" : ""}`,
             files: f.length, adds: f.reduce((a, x) => a + (x.adds || 0), 0), dels: f.reduce((a, x) => a + (x.dels || 0), 0) };
  }
  if (id === "uncommitted") return { id, sha: "", title: "Uncommitted", sub: "working tree vs HEAD", ...S.commits.uncommitted };
  const e = S.commits.entries.find((x) => x.id === id);
  return { id, sha: e.short, title: e.subject, sub: `${e.author} · ${ago(e.time)}`, files: e.files, adds: e.adds, dels: e.dels };
}
function renderPicker() {
  const ids = viewIds();
  $("#commit-pick").hidden = ids.length < 3;  // one commit and nothing else is the same as "All changes"
  if (ids.length < 3) return;
  const cur = entryOf(S.commit), i = ids.indexOf(S.commit);
  $("#cp-sha").textContent = cur.sha;
  $("#cp-sha").hidden = !cur.sha;
  $("#cp-title").textContent = cur.title;
  const n = S.commits.entries.length;
  $("#cp-pos").textContent = S.commit === "all" ? `${n} commit${n === 1 ? "" : "s"}` : `${i}/${ids.length - 1}`;
  $("#cp-pos").title = S.commit === "all" ? "commits in this review" : "position in the review";
  $("#cp-btn").title = `${cur.sha ? cur.sha + " " : ""}${cur.title}\n${cur.sub}`;
  $("#cp-prev").disabled = i <= 0;
  $("#cp-next").disabled = i >= ids.length - 1;
  if (!$("#cp-menu").hidden) renderPickerMenu();
}
function renderPickerMenu() {
  const ids = viewIds();
  $("#cp-menu").innerHTML = ids.map((id, k) => {
    const e = entryOf(id);
    const sep = k === 1 || (id === "uncommitted" && k > 1) ? '<div class="cp-sep" role="presentation"></div>' : "";
    return sep + `<div class="cp-item${id === S.commit ? " active" : ""}${id === "all" || id === "uncommitted" ? " special" : ""}" role="option" tabindex="-1" aria-selected="${id === S.commit}" data-c="${esc(id)}">` +
      `<div class="cp-line">${e.sha ? `<span class="cp-sha">${esc(e.sha)}</span>` : ""}<span class="cp-name">${esc(e.title)}</span>` +
      `<span class="cp-stat"><span class="a">+${e.adds || 0}</span> <span class="d">−${e.dels || 0}</span></span></div>` +
      `<div class="cp-sub">${esc(e.files || 0)} file${e.files === 1 ? "" : "s"} · ${esc(e.sub)}</div></div>`;
  }).join("") + (S.commits.omitted ? `<div class="cp-note">${S.commits.omitted} older commit${S.commits.omitted === 1 ? "" : "s"} only in All changes</div>` : "")
    + (S.commits.merges ? `<div class="cp-note">${S.commits.merges} merge commit${S.commits.merges === 1 ? "" : "s"} not listed</div>` : "");
}
function pickerMenu(open) {
  const menu = $("#cp-menu");
  menu.hidden = !open;
  $("#cp-btn").setAttribute("aria-expanded", String(open));
  if (!open) return;
  renderPickerMenu();
  const r = $("#commit-pick").getBoundingClientRect();
  Object.assign(menu.style, { top: `${Math.round(r.bottom + 4)}px`, left: `${Math.round(r.left)}px`, minWidth: `${Math.round(r.width)}px`,
                              maxWidth: `${Math.max(Math.round(r.width), Math.min(600, window.innerWidth - r.left - 16))}px` });
  const cur = $(".cp-item.active", menu) || $(".cp-item", menu);
  cur?.focus();
  cur?.scrollIntoView({ block: "nearest" });
}
function wirePicker() {
  $("#cp-btn").addEventListener("click", () => pickerMenu($("#cp-menu").hidden));
  $("#cp-prev").addEventListener("click", () => stepCommit(-1));
  $("#cp-next").addEventListener("click", () => stepCommit(1));
  const menu = $("#cp-menu");
  menu.addEventListener("click", (e) => {
    const it = e.target.closest(".cp-item");
    if (!it) return;
    pickerMenu(false);
    $("#cp-btn").focus();
    setCommit(it.dataset.c);
  });
  menu.addEventListener("keydown", (e) => {
    const items = $$(".cp-item", menu), at = items.indexOf(document.activeElement);
    const go = (k) => { const el = items[Math.max(0, Math.min(items.length - 1, k))]; el?.focus(); el?.scrollIntoView({ block: "nearest" }); };
    if (e.key === "ArrowDown" || e.key === "j") { e.preventDefault(); go(at + 1); }
    else if (e.key === "ArrowUp" || e.key === "k") { e.preventDefault(); go(at - 1); }
    else if (e.key === "Home") { e.preventDefault(); go(0); }
    else if (e.key === "End") { e.preventDefault(); go(items.length - 1); }
    else if (e.key === "Enter" || e.key === " ") { e.preventDefault(); document.activeElement?.click(); }
    else if (e.key === "Escape" || e.key === "Tab") { e.preventDefault(); pickerMenu(false); $("#cp-btn").focus(); }
  });
  document.addEventListener("mousedown", (e) => { if (!menu.hidden && !e.target.closest("#commit-pick")) pickerMenu(false); });
  window.addEventListener("resize", () => { if (!menu.hidden) pickerMenu(false); });
  // [ and ] step through the commits from anywhere outside a text field, like a review tool's prev/next
  document.addEventListener("keydown", (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey || (e.key !== "[" && e.key !== "]") || $("#commit-pick").hidden) return;
    const t = e.target;
    if (t.closest?.("input, textarea, [contenteditable], .inline-edit, #search")) return;
    e.preventDefault();
    stepCommit(e.key === "]" ? 1 : -1);
  });
}

function renderTray() {
  const tray = S.state.tray || [];
  $("#tray-wrap").hidden = !tray.length;
  $("#tray-count").textContent = tray.length || "";
  $("#tray").innerHTML = tray.map((t) => `<li draggable="true" data-ref="${esc(t.ref)}"><span class="ref">${esc(t.ref)}</span>${t.note ? `<span class="note">${esc(t.note)}</span>` : ""}</li>`).join("");
  const refs = tray.map((t) => t.ref);
  if (S.trayKnown === null) {
    refs.forEach((r) => addTab(parseRef(r).path));
    S.trayKnown = new Set(refs);
    renderTabs();
    return;
  }
  const fresh = tray.filter((t) => !S.trayKnown.has(t.ref));
  fresh.forEach((t) => {
    S.trayKnown.add(t.ref);
    const ref = parseRef(t.ref);
    if (t.focus) openRef(ref); else addTab(ref.path);
  });
  if (fresh.length) { renderTabs(); toast(`Reviewer pinned ${fresh.map((t) => t.ref).join(", ")}`); }
}

// path[:a-b], as agents write it: optionally old:path (the removed side) and @<commit> (lines as of that commit)
function parseRef(ref) {
  const m = String(ref).match(/^(old:)?(.*?)(?::(\d+)(?:-(\d+))?)?(?:@([0-9a-f]{7,64}))?$/);
  return { path: m[2], range: m[3] ? (m[4] ? `${m[3]}-${m[4]}` : m[3]) : null, side: m[1] ? "old" : "new", commit: m[5] || null };
}

// ------------------------------------------------------------------ tabs
// Story is pinned first; file tabs keep the order the user drags them into.
function addTab(path, at) {
  if (!path || S.tabs.includes(path)) return;
  if (at == null || at < 1 || at > S.tabs.length) S.tabs.push(path);
  else S.tabs.splice(at, 0, path);
}

// A tab shows its file name, plus the parent folder when two open tabs share that name.
function tabLabel(path, all) {
  const name = path.split("/").pop();
  const twin = all.some((t) => t !== path && isCode(t) && t.split("/").pop() === name);
  const parent = path.split("/").slice(-2, -1)[0];
  return twin && parent ? `<span class="dir">${esc(parent)}/</span>${esc(name)}` : esc(name);
}

function renderTabs() {
  // mid-drag, the strip's elements are the drag's state: rebuilding them would leave the dragged element
  // detached, and re-inserting it duplicated the tab. Render once the drop settles instead.
  if (S.tabDrag && S.tabDrag.moved) { S.tabDrag.stale = true; return; }
  $("#tabs").innerHTML = S.tabs.map((t) => t === "story"
    ? `<div class="tab story-tab${S.active === t ? " active" : ""}" role="tab" tabindex="0" aria-selected="${S.active === t}" data-tab="story">Story</div>`
    : isPage(t) ? pageTab(t)
    : `<div class="tab${S.active === t ? " active" : ""}" role="tab" tabindex="0" aria-selected="${S.active === t}" data-tab="${esc(t)}" title="${esc(t)}">` +
      `<span class="name">${tabLabel(t, S.tabs)}</span><button type="button" class="close" tabindex="-1" data-close="${esc(t)}" aria-label="close ${esc(t)}">×</button></div>`).join("");
  $$(".file").forEach((el) => el.classList.toggle("active", el.dataset.path === S.active));
  $$("#pages li").forEach((li) => li.classList.toggle("active", S.active === `page:${li.dataset.page}`));
  revealTab();
  tabOverflow();
}

function pageTab(t) {
  const pg = pageOf(t);
  const name = pg ? pageName(pg) : t.slice(5);
  return `<div class="tab page-tab${S.active === t ? " active" : ""}" role="tab" tabindex="0" aria-selected="${S.active === t}" data-tab="${esc(t)}" title="${esc(pg ? pg.shown : name)} (read-only page)">` +
    `<span class="pg-glyph" aria-hidden="true"></span><span class="name">${esc(name)}</span><button type="button" class="close" tabindex="-1" data-close="${esc(t)}" aria-label="close ${esc(name)}">×</button></div>`;
}

// Keep the active tab inside the strip, clear of the faded edges.
function revealTab() {
  const strip = $("#tabs"), el = $(".tab.active", strip);
  if (!el) return;
  const pad = 28, l = el.offsetLeft, r = l + el.offsetWidth;
  if (l - pad < strip.scrollLeft) strip.scrollLeft = l - pad;
  else if (r + pad > strip.scrollLeft + strip.clientWidth) strip.scrollLeft = r + pad - strip.clientWidth;
}

// Fade whichever edge hides tabs, and offer the "+N" menu while any tab is out of view.
function tabOverflow() {
  const strip = $("#tabs"), more = $("#tab-more");
  const max = strip.scrollWidth - strip.clientWidth;
  strip.classList.toggle("fade-l", strip.scrollLeft > 1);
  strip.classList.toggle("fade-r", strip.scrollLeft < max - 1);
  const box = strip.getBoundingClientRect();
  const hidden = $$(".tab", strip).filter((t) => { const r = t.getBoundingClientRect(); return r.left < box.left - 1 || r.right > box.right + 1; }).length;
  more.hidden = max <= 1;
  more.textContent = hidden ? `+${hidden}` : "⋯";
  more.title = hidden ? `${hidden} more open file${hidden === 1 ? "" : "s"}` : "All open files";
  if (!$("#tab-menu").hidden) renderTabMenu();
}

function renderTabMenu() {
  $("#tab-menu").innerHTML = S.tabs.map((t) => `<div class="tm-item${S.active === t ? " active" : ""}" role="menuitem" tabindex="-1" data-tab="${esc(t)}">` +
    (t === "story" ? '<span class="tm-name">Story</span>' : isPage(t)
      ? `<span class="tm-name"><span class="pg-glyph" aria-hidden="true"></span>${esc(pageOf(t) ? pageName(pageOf(t)) : t)}</span><span class="tm-dir">page</span>` +
        `<button type="button" class="close" data-close="${esc(t)}" aria-label="close ${esc(t)}">×</button>`
      : `<span class="tm-name">${esc(t.split("/").pop())}</span><span class="tm-dir">${esc(t.split("/").slice(0, -1).join("/"))}</span>` +
      `<button type="button" class="close" data-close="${esc(t)}" aria-label="close ${esc(t)}">×</button>`) + "</div>").join("");
}
function tabMenu(open) {
  const menu = $("#tab-menu");
  menu.hidden = !open;
  $("#tab-more").setAttribute("aria-expanded", String(open));
  if (open) renderTabMenu();
}

function activateTab(path) {
  if (S.active === path) return revealTab();
  S.active = path;
  renderTabs();
  renderCode();
}

// Move a file tab by one place (keyboard reorder); Story stays first.
function shiftTab(path, by) {
  const i = S.tabs.indexOf(path), j = i + by;
  if (i < 1 || j < 1 || j >= S.tabs.length) return;
  S.tabs.splice(i, 1);
  S.tabs.splice(j, 0, path);
  renderTabs();
  $(`.tab[data-tab="${CSS.escape(path)}"]`)?.focus();
}

// The index a drop at clientX lands on, between the file tabs (never before Story).
function tabSlot(x, skip) {
  const tabs = $$("#tabs .tab").filter((t) => t !== skip);
  let i = tabs.findIndex((t) => { const r = t.getBoundingClientRect(); return x < r.left + r.width / 2; });
  if (i < 0) i = tabs.length;
  return Math.max(1, i);
}

// Open a reference from outside the file tree (a pin, a chip, a link, a search hit). Its lines belong to one
// version: the commit it names, else the working tree. A commit's view would show other lines, so the
// picker moves to the view the reference was written against first.
async function openRef(ref, opts = {}) {
  let path = ref.path;
  const hit = ref.commit && S.commits.entries.find((e) => e.id.startsWith(ref.commit));
  if (ref.commit && !hit && !ref.range) path = `${path}@${ref.commit}`;  // a path that ends in @hex, not a commit
  if (hit) await setCommit(hit.id, { quiet: true, why: `Showing ${hit.short}, where those lines are` });
  else if (isSha(S.commit)) await setCommit("all", { quiet: true, why: "Showing all changes: that reference is to the working tree" });
  const view = opts.view || (S.byPath.has(path) ? undefined : "file");  // a line outside the diff is only in the whole file
  return openFile(path, { range: ref.range, side: ref.side || "new", view });
}

async function openFile(path, opts = {}) {
  addTab(path);
  S.active = path;
  if (opts.view) S.view = opts.view;
  renderTabs();
  await renderCode(opts);
}

function closeTab(path) {
  const i = S.tabs.indexOf(path);
  if (i < 0) return;
  if (isPage(path)) {  // closing a page is durable: the agent sees it, and a reload keeps it closed
    post("pages", { op: "close", id: path.slice(5) }).catch((err) => toast(err.message, true));
    dropFrame(path);
  }
  S.tabs.splice(i, 1);
  const was = S.active;
  if (S.active === path) S.active = S.tabs[Math.max(0, i - 1)] || "story";
  renderTabs();
  if (S.active !== was) renderCode();
}

// ------------------------------------------------------------------ code
// A request that never reached the server (it is restarting) resolves to OFFLINE and is not cached, so the
// view recovers once the server is back; only a real answer ("not readable") is remembered.
const OFFLINE = false;
const unreachable = (err) => err instanceof TypeError;  // fetch rejects with TypeError when nothing answers
async function getDiff(f) {
  const c = S.commit, key = `${c}:${f.n}`;
  if (!S.diffs.has(key)) {
    S.diffs.set(key, api(`diff/${f.n}${c === "all" ? "" : `?c=${c}`}`).then((d) => d.rows || [])
      .catch((err) => { if (unreachable(err)) { S.diffs.delete(key); return OFFLINE; } return []; }));
  }
  return S.diffs.get(key);
}
// a file as the current view shows it: as of the picked commit, otherwise the working tree
async function getFile(path) {
  const at = isSha(S.commit) ? S.commit : "", key = `${at}:${path}`;
  if (!S.files.has(key)) {
    S.files.set(key, api(`file?path=${encodeURIComponent(path)}${at ? `&c=${at}` : ""}`).then((d) => d.lines)
      .catch((err) => { if (unreachable(err)) { S.files.delete(key); return OFFLINE; } return null; }));
  }
  return S.files.get(key);
}
const OFFLINE_NOTE = '<p class="empty">The desk server is not answering (it may be restarting). This view reloads when it is back.</p>';

function editorHref(path, line) {
  const ed = (S.state && S.state.session.editor) || "vscode";
  const root = S.all.link_root || (S.state && S.state.session.repo) || "";
  if (ed === "none") return null;
  if (ed === "file") return `file://${root}/${path}`;
  return `${ed}://file${root}/${path}${line ? ":" + line : ""}`;
}

async function renderCode(opts = {}) {
  hideSel();
  closePop();
  if (S.sel && S.sel.path !== S.active) setSel(null);
  const story = S.active === "story", page = isPage(S.active);
  $("#story").hidden = !story;
  showFrame(page ? S.active : null);
  $("#code").hidden = story || page;
  $("#toolbar").hidden = story;
  $("#toolbar").dataset.mode = page ? "page" : "code";
  $("#tb-open").removeAttribute("target");
  if (story) return refreshFind();
  if (page) { pageBar(); return refreshFind(); }
  const path = S.active, at = S.commit;
  const stale = () => S.active !== path || S.commit !== at;
  const f = S.byPath.get(path);
  const deleted = f && f.badge === "D";
  if (!f) S.view = "file";
  if (deleted) S.view = "diff";
  const entry = isSha(at) && S.commits.entries.find((e) => e.id === at);
  $("#tb-path").innerHTML = esc(f && f.old_path ? `${f.old_path} → ${path}` : path) +
    (entry ? `<span class="tb-at" title="${esc(entry.subject)}">at ${esc(entry.short)}</span>` : "");
  $$("#toolbar [data-view]").forEach((b) => {
    b.setAttribute("aria-pressed", String(b.dataset.view === S.view));
    b.disabled = (b.dataset.view === "diff" && !f) || (b.dataset.view === "file" && deleted);
  });
  $$("#layout-seg button").forEach((b) => { b.setAttribute("aria-pressed", String(b.dataset.layout === S.layout)); b.disabled = S.view !== "diff"; });
  $("#tb-render").hidden = !/\.html?$/i.test(path) || deleted;
  const href = editorHref(path, opts.range ? +opts.range.split("-")[0] : null);
  $("#tb-open").hidden = !href || deleted;
  if (href) $("#tb-open").href = href;

  const box = $("#code");
  const lang = langFor(path);
  const cells = [];
  let html;
  if (S.view === "diff" && f) {
    const rows = await getDiff(f);
    if (stale()) return;
    if (rows === OFFLINE) { box.innerHTML = OFFLINE_NOTE; return refreshFind(); }
    if (!rows.length) html = '<p class="empty">No line changes (binary, mode or rename only).</p>';
    else if (S.layout === "split") html = splitHtml(rows);
    else {
      const single = !rows.some((r) => r[0]) || !rows.some((r) => r[1]);
      html = `<div class="rows${single ? " single" : ""}">` + rows.map((r) => {
        const cls = r[3] || "";
        const nos = single ? `<span class="no" data-side="${r[1] ? "new" : "old"}">${esc(r[1] || r[0])}</span>`
          : `<span class="no" data-side="old">${esc(r[0])}</span><span class="no" data-side="new">${esc(r[1])}</span>`;
        return `<div class="row ${esc(cls)}" data-o="${esc(r[0])}" data-n="${esc(r[1])}">${nos}<span class="tx">${esc(r[2]) || "​"}</span></div>`;
      }).join("") + "</div>";
    }
    box.innerHTML = html;
    box.dataset.path = path;
    if (S.layout !== "split") $$(".row", box).forEach((el) => cells.push({ el: $(".tx", el), text: el.querySelector(".tx").textContent.replace(/​/g, ""), cls: [...el.classList].filter((c) => c !== "row").join(" ") }));
    else $$(".half .tx", box).forEach((el) => cells.push({ el, text: el.textContent.replace(/​/g, ""), cls: el.parentElement.dataset.cls || "" }));
  } else {
    const lines = await getFile(path);
    if (stale()) return;
    if (lines === OFFLINE) { box.innerHTML = OFFLINE_NOTE; return refreshFind(); }
    if (!lines) { box.innerHTML = `<p class="empty">This file cannot be shown (${entry ? `not in ${esc(entry.short)}, ` : "missing, "}binary or too large).</p>`; return refreshFind(); }
    const changed = new Set();
    if (f) ((await getDiff(f)) || []).forEach((r) => { if (r[3] === "add") changed.add(+r[1]); });
    box.innerHTML = '<div class="rows file-view">' + lines.map((t, i) =>
      `<div class="row${changed.has(i + 1) ? " add" : ""}" data-n="${i + 1}"><span class="no" data-side="new">${i + 1}</span><span class="tx">${esc(t) || "​"}</span></div>`).join("") + "</div>";
    box.dataset.path = path;
    $$(".row", box).forEach((el) => cells.push({ el: $(".tx", el), text: el.querySelector(".tx").textContent.replace(/​/g, ""), cls: "" }));
  }
  paint(cells, lang);
  if (!opts.range) box.scrollTop = 0;
  else if (!revealRange(opts.range, opts.side || "new") && S.view === "diff" && !deleted) {
    S.view = "file";  // the range is outside every hunk: show it in the whole file
    return renderCode(opts);
  }
  refreshFind();
}

function splitHtml(rows) {
  const out = [];
  const half = (no, text, cls, side) => no || text
    ? `<div class="half ${cls}" data-cls="${cls}"><span class="no" data-side="${side}">${esc(no)}</span><span class="tx">${esc(text) || "​"}</span></div>`
    : '<div class="half pad"></div>';
  let dels = [], adds = [];
  const flush = () => {
    for (let i = 0; i < Math.max(dels.length, adds.length); i++) {
      const d = dels[i], a = adds[i];
      out.push(`<div class="split-row" data-o="${d ? esc(d[0]) : ""}" data-n="${a ? esc(a[1]) : ""}">${d ? half(d[0], d[2], "del", "old") : half()}${a ? half(a[1], a[2], "add", "new") : half()}</div>`);
    }
    dels = []; adds = [];
  };
  rows.forEach((r) => {
    const cls = r[3] || "";
    if (cls === "del") { if (adds.length) flush(); dels.push(r); }
    else if (cls === "add") adds.push(r);
    else {
      flush();
      if (cls.startsWith("hunk") || cls === "meta") out.push(`<div class="split-row ${cls.startsWith("hunk") ? "hunk" : "meta"}"><div class="half" data-cls="${esc(cls)}"><span class="tx">${esc(r[2])}</span></div></div>`);
      else out.push(`<div class="split-row" data-o="${esc(r[0])}" data-n="${esc(r[1])}">${half(r[0], r[2], "", "old")}${half(r[1], r[2], "", "new")}</div>`);
    }
  });
  flush();
  return `<div class="rows split">${out.join("")}</div>`;
}

function rowsFor(side, a, b) {
  const key = side === "old" ? "o" : "n";
  const box = $("#code");
  if ($(".rows.split", box)) {
    return $$(".split-row", box).filter((r) => { const v = +r.dataset[key]; return v && v >= a && v <= b; })
      .map((r) => r.children[side === "old" ? 0 : 1]);
  }
  return $$(".row", box).filter((r) => { const v = +r.dataset[key]; return v && v >= a && v <= b; });
}

function revealRange(range, side) {
  const [a, b = a] = String(range).split("-").map(Number);
  const els = rowsFor(side, a, b);
  if (!els.length) return false;
  els[0].scrollIntoView({ block: "center" });
  els.forEach((el) => { el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash"); });
  return true;
}

// -------------------------------------------------------------- selection
function setSel(sel) {
  S.sel = sel;
  $$("#code .sel").forEach((el) => el.classList.remove("sel"));
  // the current selection is always attached to the next message as a live [path@a-b] chip
  const live = sel ? withCommit({ path: sel.path, range: selRange(), side: sel.side }) : null;
  if (JSON.stringify(live) !== JSON.stringify(S.live)) { S.live = live; renderAnchors(); }
  if (!sel) { S.selSource = null; return hideSel(); }
  const [a, b] = [Math.min(sel.a, sel.b), Math.max(sel.a, sel.b)];
  const els = rowsFor(sel.side, a, b);
  els.forEach((el) => el.classList.add("sel"));
  if (S.dragging || !els.length || S.edit) return;  // the open flag/edit panel replaces the bar
  const bar = $("#selbar");
  $("#sel-label").textContent = `${sel.side === "old" ? "old " : ""}${a === b ? "L" + a : "L" + a + "-" + b}`;
  bar.hidden = false;
  placeNear(bar, els[els.length - 1]);
}
function selRange() {
  const s = S.sel;
  if (!s) return null;
  const a = Math.min(s.a, s.b), b = Math.max(s.a, s.b);
  return a === b ? `${a}` : `${a}-${b}`;
}
function hideSel() { $("#selbar").hidden = true; }
function placeNear(el, row) {
  const host = $("#editor").getBoundingClientRect();
  const r = row.getBoundingClientRect();
  const top = Math.min(r.bottom - host.top + 6, host.height - el.offsetHeight - 12);
  el.style.top = `${Math.max(48, top)}px`;
  el.style.left = `${Math.max(12, Math.min(140, host.width - el.offsetWidth - 12))}px`;
}

// Flag / edit opens an inline panel docked under the selected lines, the full width of the visible
// code pane. Leaving the pre-filled code unchanged files a flag; editing it files a suggested edit.
function fitArea(ta, cap = 0.6) {
  ta.style.height = "0px";
  const h = Math.min(ta.scrollHeight + 2, Math.round(window.innerHeight * cap));
  ta.style.height = `${h}px`;
  ta.style.overflowY = ta.scrollHeight + 2 > h ? "auto" : "hidden";
}

function paintEdit() {
  const ta = $("#pop-code"), hl = $("#pop-code-hl"), nums = $("#pop-nums");
  if (!ta || !S.edit) return;
  const lang = langFor(S.edit.path);
  const text = ta.value;
  // one block per logical line, so the gutter can follow wrapped lines exactly
  const lines = window.hljs && lang && hljs.getLanguage(lang)
    ? splitLines(hljs.highlight(text, { language: lang, ignoreIllegals: true }).value) : text.split("\n").map(esc);
  hl.innerHTML = lines.map((l) => `<span class="l">${l || "\u200b"}</span>`).join("");
  fitArea(ta);
  const heights = [...hl.children].map((el) => el.offsetHeight);
  nums.innerHTML = heights.map((h, k) => `<span style="height:${h}px">${S.edit.lo + k}</span>`).join("");
  syncEditScroll();
  const changed = S.edit.orig !== null && text !== S.edit.orig;
  $("#pop").classList.toggle("changed", changed);
  rowsFor(S.edit.side, S.edit.lo, S.edit.hi).forEach((r) => r.classList.toggle("replaced", changed));
  $("#pop-submit").textContent = changed ? "Suggest edit" : "Flag";
  $("#pop-hint").textContent = changed ? "suggestion · replaces the red lines" : "edit to suggest a change, or flag as is";
}
function syncEditScroll() {
  const ta = $("#pop-code"), pre = ta && ta.previousElementSibling, nums = $("#pop-nums");
  if (pre) { pre.scrollTop = ta.scrollTop; pre.scrollLeft = ta.scrollLeft; }
  if (nums) nums.scrollTop = ta.scrollTop;
}

function closePop() {
  const el = $("#pop");
  if (el) el.remove();
  $$("#code .replaced").forEach((r) => r.classList.remove("replaced"));
  S.edit = null;
}

function openPop() {
  const s = S.sel;
  if (!s) return;
  closePop();
  const lo = Math.min(s.a, s.b), hi = Math.max(s.a, s.b);
  const rows = rowsFor(s.side, lo, hi);
  if (!rows.length) return;
  const editable = s.side !== "old";  // removed lines can only be flagged
  const wrap = loadLayout().wrapEdit !== false;
  S.edit = { path: S.active, range: selRange(), side: s.side, orig: null, lo, hi };
  // the editor's text column starts exactly where the code's text column does
  const last = rows[rows.length - 1];
  const tx = last.querySelector(".tx");
  const gutter = tx ? Math.round(tx.getBoundingClientRect().left - last.getBoundingClientRect().left) : 56;
  const el = document.createElement("div");
  el.className = "inline-edit";
  el.id = "pop";
  el.style.setProperty("--gutter-w", `${gutter}px`);
  const where = `L${lo}${hi !== lo ? "-" + hi : ""}`;
  el.innerHTML =
    `<div class="ie-bar"><span class="ie-tag">${editable ? "flag or edit" : "flag"} · ${where}</span>` +
    `<span class="hint" id="pop-hint">${editable ? "edit to suggest a change, or flag as is" : "removed lines can only be flagged"}</span><span class="grow"></span>` +
    (editable ? `<button type="button" class="ie-wrap" aria-pressed="${wrap}" title="Soft-wrap long lines">wrap</button>` : "") +
    `<button type="button" class="x" data-act="cancel" aria-label="cancel (Esc)">×</button></div>` +
    (editable ? `<div class="ie-body"><div class="ie-nums" id="pop-nums" aria-hidden="true"></div>` +
      `<div class="code-edit${wrap ? " wrap" : ""}" id="pop-edit"><pre aria-hidden="true"><code id="pop-code-hl"></code></pre>` +
      `<textarea id="pop-code" spellcheck="false" autocapitalize="off" autocomplete="off" wrap="${wrap ? "soft" : "off"}" aria-label="suggested replacement for ${where}"></textarea></div></div>` : "") +
    `<div class="ie-foot"><textarea id="pop-note" class="ie-note" rows="1" placeholder="Why? (optional)"></textarea>` +
    `<span class="keys">esc · ⌘↵</span><button type="button" class="primary" id="pop-submit">Flag</button></div>`;
  (rows[rows.length - 1].closest(".split-row") || rows[rows.length - 1]).after(el);
  hideSel();

  const note = $("#pop-note", el), ta = $("#pop-code", el);
  const submit = async () => {
    if (!S.edit) return;
    const anchor = withCommit({ path: S.edit.path, range: S.edit.range, side: S.edit.side });
    const body = { anchor, note: note.value, ...S.tier };
    const edited = ta && S.edit.orig !== null && ta.value !== S.edit.orig;
    if (edited) body.replacement = ta.value;
    try {
      const r = await post(edited ? "suggest" : "flag", body);
      closePop(); window.getSelection().removeAllRanges(); setSel(null);
      toast(`${r.item} added to the backlog`);
      refresh();
    } catch (err) { toast(err.message, true); }
  };
  const keys = (e) => {
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closePop(); }
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); submit(); }
  };
  el.addEventListener("keydown", keys);
  $("#pop-submit", el).addEventListener("click", submit);
  $('[data-act="cancel"]', el).addEventListener("click", closePop);
  note.addEventListener("input", () => fitArea(note, 0.3));
  if (!editable) { note.focus(); el.scrollIntoView({ block: "nearest" }); return; }
  $(".ie-wrap", el).addEventListener("click", (e) => {
    const on = e.currentTarget.getAttribute("aria-pressed") !== "true";
    e.currentTarget.setAttribute("aria-pressed", String(on));
    $("#pop-edit", el).classList.toggle("wrap", on);
    ta.wrap = on ? "soft" : "off";
    const l = loadLayout(); l.wrapEdit = on; saveLayout(l);
    paintEdit();
  });
  ta.addEventListener("input", paintEdit);
  ta.addEventListener("scroll", syncEditScroll);
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Tab" && !e.shiftKey) {  // indent instead of leaving the editor
      e.preventDefault();
      ta.setRangeText("    ", ta.selectionStart, ta.selectionEnd, "end");
      paintEdit();
    }
  });
  getFile(S.edit.path).then((lines) => {
    if (!S.edit || !document.body.contains(ta)) return;
    ta.value = lines ? lines.slice(lo - 1, hi).join("\n") : "";
    S.edit.orig = ta.value;
    paintEdit();
    el.scrollIntoView({ block: "nearest" });
    ta.focus();
    ta.setSelectionRange(ta.value.length, ta.value.length);
  });
}

async function analyzeSelection() {
  const s = S.sel;
  if (!s) return;
  if (S.skills.length && !S.skills.some((k) => k.name === "analyze-code")) return toast("The analyze-code skill is not installed", true);
  const range = selRange();
  try {
    const at = isSha(S.commit) ? `@${shortOf(S.commit)}` : "";
    await post("message", { text: `/analyze-code ${S.active}:${range}${at}`, anchors: [withCommit({ path: S.active, range, side: s.side })], ...S.tier });
    window.getSelection().removeAllRanges();
    setSel(null);
    setPane("chat");
    toast(`Analyzing ${S.active}:${range}`);
    refresh();
  } catch (err) { toast(err.message, true); }
}

// ------------------------------------------------------------------- chat
function userText(text) {
  const m = String(text || "").match(/^\/([\w:-]+)(\s[\s\S]*)?$/);
  const body = (t) => esc(t).replace(/\n/g, "<br>");
  return m ? `<span class="cmd">/${esc(m[1])}</span>${body(m[2] || "")}` : body(text);
}

function renderChat() {
  const st = S.state;
  const chat = st.chat || [];
  const sig = `${chat.length}|${st.unanswered.join(",")}|${chat.length ? chat[chat.length - 1].seq : 0}|${(st.agent || {}).state}|${st.session.status}`;
  if (sig === S.chatSig) return;
  S.chatSig = sig;
  const log = $("#log");
  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 80;
  const pending = new Set(st.unanswered);
  if (!chat.length) {
    log.innerHTML = `<li class="empty">Ask anything about this change; selected code attaches itself. Select lines for <b>Analyze code</b>, or <b>Flag / edit</b> to put work in the backlog (edit the lines to suggest a fix).<br><br>The reviewer answers here and never edits code; implementation goes to the main agent when you press <b>Execute</b> or the reviewer exits.</li>`;
    return;
  }
  log.innerHTML = chat.map((e) => {
    const tier = e.model ? `<span class="tierchip">${esc(tierLabel(e))}</span>` : "";
    const anchors = (e.anchors || []).map((a) => {
      const ref = a.range ? `${a.path}:${a.range}` : a.path;
      return `<span class="chip-loc" data-ref="${esc(ref)}" data-side="${esc(a.side || "new")}" data-commit="${esc(a.commit || "")}">${esc(a.range ? anchorLabel(a) : a.path)}</span>`;
    }).join("");
    const excerpt = (e.anchors || []).filter((a) => a.excerpt).map((a) => `<pre class="excerpt">${esc(a.excerpt)}</pre>`).join("");
    const wait = "";  // the typing row at the end of the log shows who is answering and for how long
    if (e.kind === "tier") return `<li class="sys">switched reviewer to <b>${esc(tierLabel(e))}</b></li>`;
    if (e.kind === "execute") return `<li class="sys">execute requested for <b>${esc((e.ids || []).join(", "))}</b></li>`;
    if (e.kind === "end") return `<li class="sys">review ended</li>`;
    if (e.kind === "flag" || e.kind === "suggest") {
      const sugg = e.suggestion ? `<pre class="excerpt">${esc(e.suggestion)}</pre>` : "";
      return `<li class="msg user"><div class="msg-head"><span class="who">you</span><span class="seq">#${e.seq}</span>${tier}${wait}</div>` +
        `<div class="card ${e.kind}"><span class="k">${e.kind === "flag" ? "flagged" : "suggested edit"} → ${esc(e.item || "")}</span>` +
        `<div class="attach">${anchors}</div>${e.text ? `<div>${esc(e.text)}</div>` : ""}${sugg}</div></li>`;
    }
    if (e.role === "agent") {
      const to = (e.reply_to || []).map((i) => `#${i}`).join(" ");
      return `<li class="msg agent"><div class="msg-head"><span class="who">reviewer</span>${tier}${to ? `<span class="seq">↳ ${esc(to)}</span>` : ""}</div><div class="bubble">${md(e.text)}</div></li>`;
    }
    return `<li class="msg user"><div class="msg-head"><span class="who">you</span><span class="seq">#${e.seq}</span>${tier}${wait}</div>` +
      `${anchors ? `<div class="attach">${anchors}</div>` : ""}${excerpt}<div class="bubble">${userText(e.text)}</div></li>`;
  }).join("");
  if (pending.size && st.session.status !== "ended") {
    const first = chat.find((e) => pending.has(e.seq));
    const w = waitingLabel(st);
    log.insertAdjacentHTML("beforeend", `<li class="typing${w.live ? " live" : ""}"><span class="dots"><i></i><i></i><i></i></span>` +
      `<span>${esc(w.text)}</span><span class="elapsed" data-since="${first ? first.at : Date.now() / 1000}"></span></li>`);
    tickWaiting();
  }
  if (atBottom || pending.size) log.scrollTop = log.scrollHeight;
  renderDraft();
}

// The reviewer's answer as it streams: a live bubble after the log, replaced by the real message.
// The head is built once and patched in place: rebuilding it on every streamed update would replace
// the spinner element and restart its rotation, so it only twitched while a long tool call streamed.
function renderDraft() {
  const log = $("#log");
  let li = $("#draft", log);
  const d = S.draft;
  if (!d || (!d.text && !d.status)) { if (li) li.remove(); log.querySelector(".typing")?.removeAttribute("hidden"); return; }
  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 120;
  if (!li) {
    li = document.createElement("li");
    li.id = "draft";
    li.className = "msg agent draft";
    li.innerHTML = '<div class="msg-head"><span class="who">reviewer</span><span class="tierchip"></span><span class="seq"></span>' +
      '<span class="status"><span class="spin"></span><span class="status-text"></span></span></div><div class="bubble"></div>';
    log.appendChild(li);
  }
  log.querySelector(".typing")?.setAttribute("hidden", "");
  const set = (sel, text) => { const el = $(sel, li); if (el.textContent !== text) el.textContent = text; return el; };
  set(".tierchip", tierLabel(d));
  set(".seq", (d.reply_to || []).length ? `↳ ${d.reply_to.map((i) => `#${i}`).join(" ")}` : "").hidden = !(d.reply_to || []).length;
  const status = d.status || "";
  set(".status-text", status).title = status;
  $(".status", li).hidden = !status;
  const bubble = $(".bubble", li);
  bubble.hidden = !d.text;
  if (d.text && bubble.dataset.text !== d.text) {
    bubble.dataset.text = d.text;
    // an unclosed code fence renders as code while it streams
    const text = (d.text.match(/^```/gm) || []).length % 2 ? d.text + "\n```" : d.text;
    bubble.innerHTML = md(text);
    // the caret follows the last streamed word: inside the deepest last paragraph, list item or code line
    let host = bubble;
    for (let next = host.lastElementChild; next; next = host.lastElementChild) {
      const block = /^(P|UL|OL|LI|PRE)$/.test(next.tagName) || (next.tagName === "CODE" && host.tagName === "PRE");
      if (!block) break;
      host = next;
    }
    host.insertAdjacentHTML("beforeend", '<span class="caret"></span>');
  }
  if (atBottom) log.scrollTop = log.scrollHeight;
}

function renderPresence() {
  const st = S.state;
  const p = st.agent || {};
  const el = $("#presence");
  let state = p.state || "offline", label;
  const queued = st.unanswered.length;
  if (st.session.status === "ended") { state = "ended"; label = "review ended"; }
  else if (state === "waiting") label = `${p.main ? "main agent" : tierLabel(p)} · ready`;
  else if (state === "thinking") label = `${p.main ? "main agent" : tierLabel(p)} · thinking…`;
  else if (state === "respawning") label = `starting ${tierLabel(p.wanted) || "reviewer"}…`;
  else if (state === "executing") label = "main agent implementing…";
  else if (state === "exited") label = `reviewer ${p.reason === "idle" ? "went idle" : "left"}${queued ? ` · ${queued} queued` : ""}`;
  else label = `no reviewer${queued ? ` · ${queued} queued` : ""}`;
  el.dataset.state = state;
  $(".label", el).textContent = label;
  el.title = state === "offline" || state === "exited"
    ? "No reviewer is attached. Messages queue on disk; ask the main agent in the terminal to resume the review." : "";
}

function renderTier() {
  const st = S.state;
  if (!S.tierTouched) {
    const p = st.agent || {};
    S.tier = (p.live && p.model) ? { model: p.model, effort: p.effort } : (p.wanted || (st.prefs.model ? st.prefs : null) || { model: "haiku", effort: "low" });
  }
  $("#models").innerHTML = st.models.map((m) => `<button type="button" data-model="${m}" aria-pressed="${S.tier.model === m}">${MODEL_LABEL[m] || m}</button>`).join("");
  $("#efforts").innerHTML = st.efforts.map((x) => `<button type="button" data-effort="${x}" aria-pressed="${S.tier.effort === x}">${x}</button>`).join("");
}

const shortOf = (c) => (S.commits.entries.find((e) => e.id === c) || {}).short || String(c).slice(0, 7);
const anchorLabel = (a) => `[${a.side === "old" ? "old:" : ""}${a.path}@${a.range}${a.commit ? ` · ${shortOf(a.commit)}` : ""}]`;
const sameAnchor = (x, y) => x && y && x.path === y.path && x.range === y.range && x.side === y.side && (x.commit || "") === (y.commit || "");
// lines picked in one commit's view are that commit's; the reviewer reads them there, not in the working tree
const withCommit = (a) => (isSha(S.commit) ? { ...a, commit: S.commit } : a);

function renderAnchors() {
  const live = S.live && !S.anchors.some((a) => sameAnchor(a, S.live))
    ? `<span class="chip-loc live" data-ref="${esc(S.live.path + ":" + S.live.range)}" data-side="${S.live.side}" data-commit="${esc(S.live.commit || "")}" title="Your current selection; it goes with the next message. Pin it with Ask to keep it after you deselect.">${esc(anchorLabel(S.live))}<button type="button" data-drop="live" aria-label="deselect">×</button></span>` : "";
  $("#anchors").innerHTML = live + S.anchors.map((a, i) => `<span class="chip-loc" data-ref="${esc(a.path + ":" + a.range)}" data-side="${a.side}" data-commit="${esc(a.commit || "")}">${esc(anchorLabel(a))}<button type="button" data-drop="${i}" aria-label="remove">×</button></span>`).join("");
}

function outgoingAnchors() {
  return S.live && !S.anchors.some((a) => sameAnchor(a, S.live)) ? [...S.anchors, S.live] : [...S.anchors];
}

// ------------------------------------------------------------ slash skills
function renderSlash() {
  const menu = $("#slash");
  const m = $("#input").value.match(/^\/([\w:-]*)$/);
  if (!m || !S.skills.length) { menu.hidden = true; return; }
  const q = m[1].toLowerCase();
  const hits = S.skills.filter((k) => k.name.toLowerCase().includes(q))
    .sort((x, y) => x.name.toLowerCase().indexOf(q) - y.name.toLowerCase().indexOf(q)).slice(0, 7);
  if (!hits.length) { menu.hidden = true; return; }
  S.slashIdx = Math.min(S.slashIdx, hits.length - 1);
  menu.innerHTML = hits.map((k, i) => `<li data-name="${esc(k.name)}" class="${i === S.slashIdx ? "on" : ""}"><b>/${esc(k.name)}</b><span>${esc(k.description)}</span></li>`).join("");
  menu.hidden = false;
}
function pickSlash(name) {
  const input = $("#input");
  input.value = `/${name} `;
  $("#slash").hidden = true;
  input.focus();
}

// --------------------------------------------------------- waiting indicator
function waitingLabel(st) {
  const p = st.agent || {};
  const who = p.main ? "main agent" : tierLabel(p) || "reviewer";
  switch (p.state) {
    case "thinking": return { text: `${who} is responding`, live: true };
    case "waiting": return { text: `${who} is picking it up`, live: true };
    case "respawning": return { text: `starting ${tierLabel(p.wanted) || "a reviewer"}`, live: true };
    case "executing": return { text: "main agent is implementing; replies resume after", live: true };
    default: return { text: "queued: no reviewer attached (ask the main agent in the terminal to resume)", live: false };
  }
}
function tickWaiting() {
  const el = $("#log .typing .elapsed");
  if (!el) return;
  const s = Math.max(0, Math.round(Date.now() / 1000 - +el.dataset.since));
  el.textContent = s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
}

// ---------------------------------------------------------------- backlog
function renderBacklog() {
  const items = (S.state.backlog || []).slice().sort((a, b) => +a.id.slice(1) - +b.id.slice(1));
  const live = items.filter((i) => !["done", "dismissed"].includes(i.status));
  const badge = $("#backlog-count");
  badge.textContent = live.length;
  badge.classList.toggle("hot", items.some((i) => i.status === "open"));
  const sig = JSON.stringify(items.map((i) => [i.id, i.status, i.title, i.detail, i.note])) + [...S.picked].join();
  if (sig === S.backlogSig) return;
  S.backlogSig = sig;
  // acked = accepted by the main agent but not finished: still selectable, so the user can execute it again or dismiss it
  const openish = (i) => ["open", "handed-off", "acked"].includes(i.status);
  S.picked.forEach((id) => { if (!items.some((i) => i.id === id && openish(i))) S.picked.delete(id); });
  $("#items").innerHTML = items.length ? items.map((i) => {
    const patch = i.patch ? `<pre class="patch">${i.patch.replace(/\n$/, "").split("\n").map((l) => `<span class="${l.startsWith("+") && !l.startsWith("+++") ? "a" : l.startsWith("-") && !l.startsWith("---") ? "d" : l.startsWith("@@") ? "h" : ""}">${esc(l)}</span>`).join("\n")}</pre>` : "";
    const acts = i.status === "dismissed" ? `<button type="button" data-reopen="${i.id}">reopen</button>`
      : openish(i) ? `<button type="button" data-dismiss="${i.id}">dismiss</button>` : "";
    return `<li class="item${openish(i) ? "" : " closed"}">` +
      `${openish(i) ? `<input type="checkbox" data-pick="${i.id}" ${S.picked.has(i.id) ? "checked" : ""} aria-label="select ${i.id}">` : "<span></span>"}` +
      `<div class="line1"><span class="iid">${esc(i.id)}</span><span class="status" data-s="${esc(i.status)}">${esc(i.status)}</span><span>${esc(i.kind || "fix")}</span><span>by ${esc(i.by || "?")}</span></div>` +
      `<div class="body"><div class="title">${esc(i.title)}</div>` +
      `${i.anchor ? `<div><span class="chip-loc" data-ref="${esc(i.anchor)}">${esc(i.anchor)}</span></div>` : ""}` +
      // Details and notes are Markdown, like chat answers: the reviewer writes Context / Issue / Suggested fix /
      // Reasoning / Tests as bullets, and backticked path:line refs become links that open in the editor.
      `${i.detail && i.detail !== i.title ? `<div class="detail">${md(i.detail)}</div>` : ""}${patch}` +
      `${i.note ? `<div class="detail note">${md(`main: ${i.note}`)}</div>` : ""}<div class="acts">${acts}</div></div></li>`;
  }).join("") : '<li class="empty">Nothing yet. Flags, suggested edits and issues the reviewer finds land here, and reach the main agent on Execute or when the reviewer exits.</li>';
  syncExecSel();
  $("#exec-all").disabled = !items.some((i) => ["open", "handed-off"].includes(i.status));  // the server skips acked work here
}

// "Execute selected" stays clickable while nothing is ticked: a disabled button swallows the click without a
// word, and users took that for a broken desk. It counts the selection and explains itself instead.
function syncExecSel() {
  const b = $("#exec-sel"), n = S.picked.size;
  b.setAttribute("aria-disabled", String(!n));
  b.textContent = n ? `Execute ${n} selected` : "Execute selected";
  b.title = n ? `Execute ${[...S.picked].join(", ")}` : "Tick the items to execute first";
}

// Where the latest Execute or End stands. The main agent gets it from its hooks while it works, or from its
// watch while idle; with neither, nothing reaches it, and the desk says so instead of implying it will.
const VIA = { "hook:tool": "while it was working", "hook:stop": "as it finished a reply", "hook:prompt": "with your next message to it", watch: "by waking it" };
function ago(t) {
  const s = Math.max(0, Math.round(Date.now() / 1000 - t));
  return s < 60 ? `${s}s ago` : s < 3600 ? `${Math.round(s / 60)}m ago` : s < 172800 ? `${Math.round(s / 3600)}h ago` : `${Math.round(s / 86400)}d ago`;
}
function renderDelivery() {
  const d = S.state.delivery || { state: "idle" }, el = $("#delivery");
  const last = d.delivered;
  const fresh = d.state !== "delivered" || (last && Date.now() / 1000 - last.at < 600);
  el.hidden = d.state === "idle" || !fresh;
  el.dataset.s = d.state;
  const cmd = `review-desk-axi handoff ${D.sid}`;
  el.innerHTML = d.state === "delivered" ? `Delivered to the main agent ${esc(ago(last.at))}, ${esc(VIA[last.via] || "")}.`.replace(", .", ".")
    : d.state === "waking" ? "Waking the main agent…"
    : d.state === "queued" ? `Queued: the main agent is busy (active ${esc(ago(d.owner.seen))}) and gets this at its next step.`
    : d.state === "unheard" ? `Not delivered: no agent is listening for this desk. In any agent, run <code>${esc(cmd)}</code><button type="button" class="copy" data-copy="${esc(cmd)}">copy</button>`
    : "";
  clearTimeout(S.deliveryTimer);  // "queued" ages into "unheard" without anything on disk changing
  if (d.state === "queued" || d.state === "waking") S.deliveryTimer = setTimeout(refresh, 15000);
}

// ---------------------------------------------------------------- refresh
async function refresh() {
  let st;
  try { st = await api("state"); } catch (e) { return; }
  const first = !S.state;
  S.state = st;
  if (first || st.session.reloaded !== S.reloaded) {
    const again = !first && S.reloaded !== st.session.reloaded;
    S.reloaded = st.session.reloaded;
    const known = new Set(S.commits.entries.map((e) => e.id));
    S.all = await api("manifest").catch(() => ({ files: [] }));
    S.commits = await api("commits").catch(() => ({ entries: [], uncommitted: null }));
    S.views.clear(); S.diffs.clear(); S.files.clear();
    if (first) S.commit = rememberedCommit();
    await setCommit(viewIds().includes(S.commit) ? S.commit : "all", { quiet: true });
    const added = again ? S.commits.entries.filter((e) => !known.has(e.id)) : [];
    $("#title").textContent = st.session.title || "Review";
    const m = S.all;
    const pr = st.session.pr, meta = $("#meta");
    meta.textContent = [pr ? "" : st.session.repo && st.session.repo.split("/").pop(), m.base_label && `vs ${m.base_label}`].filter(Boolean).join(" · ");
    if (pr) {
      // a PR desk names the pull request, not its worktree folder, and links to it on GitHub
      const a = document.createElement("a");
      a.href = pr.url; a.target = "_blank"; a.rel = "noopener noreferrer";
      a.textContent = `${pr.owner}/${pr.repo}#${pr.number}`;
      a.title = `Open the pull request on GitHub (@${pr.author}, ${pr.base_ref} ← ${pr.head_ref})`;
      meta.prepend(a, meta.textContent ? " · " : "");
    }
    const story = $("#story");
    if (first) story.src = `/s/${D.sid}/story?t=${encodeURIComponent(D.token)}`;
    else if (again) {
      story.contentWindow.location.reload();
      if (S.active !== "story") renderCode();
      toast(added.length ? `${added.length} new commit${added.length === 1 ? "" : "s"}: ${added.map((e) => e.short).join(", ")}` : "Summary and diffs reloaded");
    }
  }
  S.draft = st.draft || null;
  syncPages(first);
  renderTray(); renderChat(); renderDraft(); renderPresence(); renderTier(); renderBacklog(); renderDelivery();
  $("#send").disabled = st.session.status === "ended";
}

// ------------------------------------------------------------------ pages
// One sandboxed iframe per open page, kept while its tab is open so scroll and state survive tab switches.
// The view route serves the page under an opaque origin (CSP sandbox): its scripts cannot reach the desk.
const PAGE_SANDBOX = "allow-scripts allow-popups allow-popups-to-escape-sandbox allow-downloads allow-modals";
const frameOf = (t) => $$("#panes .page-frame").find((f) => f.dataset.tab === t);
const pageUrl = (pg) => `${S.state.view}/${pg.id}/${encodeURIComponent(pg.path.split("/").pop())}?v=${pg.mtime || 0}`;

function showFrame(t) {
  $$("#panes .page-frame, #panes .page-missing").forEach((f) => { f.hidden = f.dataset.tab !== t; });
  if (!t || frameOf(t) || $(`#panes .page-missing[data-tab="${CSS.escape(t)}"]`)) return;
  const pg = pageOf(t);
  if (!pg) return;
  if (!pg.mtime) {
    $("#panes").insertAdjacentHTML("beforeend", `<p class="empty page-missing" data-tab="${esc(t)}">${esc(pg.shown)} no longer exists.</p>`);
    return;
  }
  const f = document.createElement("iframe");
  f.className = "page-frame";
  f.dataset.tab = t;
  f.dataset.mtime = pg.mtime;
  f.title = pageName(pg);
  f.setAttribute("sandbox", PAGE_SANDBOX);
  f.setAttribute("allow", "clipboard-write");  // pages' own copy buttons (a modifier-click on a ref)
  f.src = pageUrl(pg);
  $("#panes").appendChild(f);
}
function dropFrame(t) { $$(`#panes [data-tab="${CSS.escape(t)}"]`).forEach((f) => f.remove()); }

function pageBar() {
  const pg = pageOf(S.active);
  $("#tb-path").textContent = pg ? pg.shown : S.active;
  const open = $("#tb-open");
  open.hidden = !pg || !pg.mtime;
  if (pg) { open.href = pageUrl(pg); open.target = "_blank"; }
}

// Fold the durable page list into tabs: new pages open, closed ones go, edited ones reload, and a page an
// agent opened with focus becomes the active tab (once).
function syncPages(first) {
  const pages = S.state.pages || [];
  let changed = false, jump = null;
  pages.forEach((pg) => {
    const t = `page:${pg.id}`;
    const seen = S.pageSeen.get(pg.id);
    if (pg.open && !S.tabs.includes(t)) { addTab(t); changed = true; }
    if (!pg.open && S.tabs.includes(t)) {
      const i = S.tabs.indexOf(t);
      S.tabs.splice(i, 1);
      dropFrame(t);
      if (S.active === t) { S.active = S.tabs[Math.max(0, i - 1)] || "story"; jump = jump || S.active; }
      changed = true;
    }
    if (pg.open && pg.focus && !first && (!seen || seen.focus !== pg.focus)) jump = t;
    const f = frameOf(t);
    if (f && String(pg.mtime) !== f.dataset.mtime) {
      if (pg.mtime) { f.dataset.mtime = pg.mtime; f.src = pageUrl(pg); } else dropFrame(t);
    }
    S.pageSeen.set(pg.id, { focus: pg.focus, mtime: pg.mtime });
  });
  // a desk without a summary (a pull request) has an empty Story: start on its first page instead
  const firstOpen = pages.find((pg) => pg.open);
  if (first && !S.state.session.page && S.active === "story" && firstOpen) jump = `page:${firstOpen.id}`;
  const sig = JSON.stringify(pages.map((pg) => [pg.id, pg.open, pageName(pg), pg.mtime]));
  if (sig !== S.pageSig) { S.pageSig = sig; renderPages(); }
  if (jump) {
    const pg = pageOf(jump);
    if (pg && pg.by === "reviewer" && S.active !== jump) toast(`Reviewer opened ${pageName(pg)}`);
    S.active = jump;
    renderTabs();
    renderCode();
  } else if (changed || sig !== S.renderedSig) renderTabs();  // a page's title shows in its tab
  S.renderedSig = sig;
  if (isPage(S.active)) pageBar();
}

function renderPages() {
  const pages = S.state.pages || [];
  $("#pages-wrap").hidden = !pages.length;
  $("#pages-count").textContent = pages.length || "";
  $("#pages").innerHTML = pages.map((pg) => `<li data-page="${esc(pg.id)}" class="${pg.open ? "open" : "closed"}${S.active === `page:${pg.id}` ? " active" : ""}" title="${esc(pg.shown)}">` +
    `<span class="ref"><span class="pg-glyph" aria-hidden="true"></span>${esc(pageName(pg))}</span>` +
    `<span class="note">${esc(pg.id)} · ${pg.open ? "open" : "closed"}${pg.mtime ? "" : " · file missing"}${pg.by ? ` · by ${esc(pg.by)}` : ""}</span></li>`).join("");
}

// Open an HTML file as a page tab (chat links, editor links to .html files, the rail list).
async function openPage(ref) {
  try {
    const r = await post("pages", { op: "open", ...(/^P\d+$/.test(ref) ? { id: ref } : { path: ref }) });
    S.pageSeen.delete(r.id);  // focus it as soon as the state arrives
    await refresh();
    activateTab(`page:${r.id}`);
  } catch (err) { toast(err.message, true); }
}

// An editor link from the Story page or a page: a repository file opens in the editor, an .html file
// opens as a page, anything else goes to the external editor as before.
function openHref(href, title = "") {
  const root = (S.all.link_root || "").replace(/\/$/, "");
  const m = href.match(/^(?:vscode|cursor|windsurf|zed):\/\/file(\/[^:]+)(?::(\d+))?/) || href.match(/^file:\/\/(\/[^?#]+)$/);
  if (!m) return false;
  const abs = decodeURIComponent(m[1]);
  if (/\.html?$/i.test(abs)) { openPage(abs); return true; }
  if (!root || !abs.startsWith(root + "/")) return false;
  const path = abs.slice(root.length + 1);
  const line = m[2] || (title.match(/:(\d+)/) || [])[1];
  const span = (title.match(/:(\d+)-(\d+)$/) || []);
  openRef({ path, range: line ? (span[2] ? `${span[1]}-${span[2]}` : `${line}`) : null }, { view: line ? undefined : "diff" });
  return true;
}

// ------------------------------------------------------------------ wiring
function onLocClick(e) {
  const pl = e.target.closest("a.page-link[data-page-path]");
  if (pl) {
    e.preventDefault();
    // sent as written: the server looks a relative name up in the session's pages folder, then the repository
    return openPage(pl.dataset.pagePath);
  }
  const el = e.target.closest("#tray li[data-ref], .chip-loc[data-ref], a.loc[data-ref]");
  if (!el || e.target.closest("[data-drop]")) return;
  e.preventDefault();
  const ref = parseRef(el.dataset.ref);
  if (el.dataset.side) ref.side = el.dataset.side;
  if (el.dataset.commit !== undefined) ref.commit = el.dataset.commit || null;
  openRef(ref);
}

function wireStory() {
  const fr = $("#story");
  fr.addEventListener("load", () => {
    let doc;
    try { doc = fr.contentDocument; } catch (_) { return; }
    if (!doc) return;
    doc.addEventListener("click", (e) => {
      const a = e.target.closest("a[href]");
      if (!a || e.metaKey || e.ctrlKey) return;
      if (openHref(a.getAttribute("href"), a.title)) e.preventDefault();
    }, true);
  });
  // pages are sandboxed, so their injected bridge posts editor links here instead;
  // a summary's glossary cards (in the story or a page tab) post "ask" with the term or selection
  window.addEventListener("message", (e) => {
    const d = e.data;
    if (!d || typeof d !== "object") return;
    if (d.desk === "ask" && (e.source === fr.contentWindow || $$("#panes .page-frame").some((f) => f.contentWindow === e.source))) return askFromPage(d);
    if (!$$("#panes .page-frame").some((f) => f.contentWindow === e.source)) return;
    if (d.desk === "ref" && typeof d.ref === "string" && /^[\w.\/@+-]+(:\d+(-\d+)?)?$/.test(d.ref)) {
      return openRef(parseRef(d.ref));
    }
    if (d.desk !== "open" || typeof d.href !== "string") return;
    // outside the repo: hand it to the external editor, but never follow anything but an editor scheme
    if (!openHref(d.href, String(d.title || "")) && /^(?:vscode|cursor|windsurf|zed):\/\/file\//.test(d.href)) location.href = d.href;
  });
}

// "Ask Claude" on a page: quote what was asked about and where, then hand the composer to the user.
// Nothing is sent until they press Enter, so a page can never post to the chat by itself.
function askFromPage(d) {
  const clip = (v, n) => String(v || "").replace(/\s+/g, " ").trim().slice(0, n);
  const quote = clip(d.quote, 600), section = clip(d.section, 160), question = clip(d.question, 600);
  if (!quote && !question) return;
  setPane("chat");
  const input = $("#input");
  const head = quote ? `> ${quote}${section ? `\n> (${section})` : ""}\n\n` : "";
  input.value = head + question;
  input.dispatchEvent(new Event("input"));
  input.focus();
  input.setSelectionRange(head.length, input.value.length);  // typing replaces the suggested question
  toast("Question ready in the chat: edit it or press Enter");
}

function wire() {
  document.addEventListener("click", onLocClick);
  wireTabs();
  $("#pages").addEventListener("click", (e) => {
    const li = e.target.closest("[data-page]");
    if (!li) return;
    const pg = pageOf(`page:${li.dataset.page}`);
    if (pg && pg.open) activateTab(`page:${pg.id}`); else openPage(li.dataset.page);
  });
  $("#tb-reload").addEventListener("click", () => {
    const f = frameOf(S.active);
    if (f) f.src = f.src;  // the sandboxed frame's own location is not ours to reload
  });
  $("#tb-render").addEventListener("click", () => {
    const root = S.all.link_root || (S.state && S.state.session.repo) || "";
    openPage(`${root}/${S.active}`);
  });
  $("#tree").addEventListener("click", (e) => {
    const h = e.target.closest(".dir-head");
    if (h) { const d = h.parentElement.dataset.dir; S.closedDirs.has(d) ? S.closedDirs.delete(d) : S.closedDirs.add(d); return renderTree(); }
    const f = e.target.closest(".file");
    if (f) openFile(f.dataset.path, { view: "diff" });
  });
  $("#filter").addEventListener("input", (e) => { S.filter = e.target.value; renderTree(); });

  // drag files (tree or tray) into the editor
  document.addEventListener("dragstart", (e) => {
    const f = e.target.closest(".file, #tray li");
    if (!f) return;
    e.dataTransfer.setData("text/x-desk-ref", f.dataset.path || f.dataset.ref);
    e.dataTransfer.effectAllowed = "copy";
  });
  // over the editor a file opens as the last tab; over the tab strip it lands where it is dropped
  const zones = [$("#panes"), $("#tabbar")];
  const mark = (at) => $$("#tabs .tab").forEach((t, i) => t.classList.toggle("drop-before", at != null && i === at));
  zones.forEach((z) => {
    z.addEventListener("dragover", (e) => {
      if (!e.dataTransfer.types.includes("text/x-desk-ref")) return;
      e.preventDefault();
      z.classList.add("drop");
      if (z.id === "tabbar") { const at = tabSlot(e.clientX); mark(at); $("#tabs").classList.toggle("drop-end", at >= S.tabs.length); }
    });
    z.addEventListener("dragleave", (e) => { if (!z.contains(e.relatedTarget)) { z.classList.remove("drop"); mark(null); $("#tabs").classList.remove("drop-end"); } });
    z.addEventListener("drop", (e) => {
      z.classList.remove("drop");
      mark(null);
      $("#tabs").classList.remove("drop-end");
      const ref = e.dataTransfer.getData("text/x-desk-ref");
      if (!ref) return;
      e.preventDefault();
      const r = parseRef(ref);
      if (z.id === "tabbar" && !S.tabs.includes(r.path)) addTab(r.path, tabSlot(e.clientX));
      openFile(r.path, { range: r.range });
    });
  });

  $("#toolbar").addEventListener("click", (e) => {
    const v = e.target.closest("[data-view]"), l = e.target.closest("[data-layout]");
    if (v && !v.disabled) { S.view = v.dataset.view; renderCode(); }
    if (l && !l.disabled) { S.layout = l.dataset.layout; renderCode(); }
  });

  // line selection: press on a line number, drag, shift-click to extend
  const code = $("#code");
  code.addEventListener("mousedown", (e) => {
    if (e.target.closest(".inline-edit")) return;
    const no = e.target.closest(".no");
    if (!no || !no.textContent.trim() || e.button !== 0) {
      if (e.button === 0 && S.selSource === "gutter") setSel(null);  // clicking the code unhighlights
      return;
    }
    e.preventDefault();
    window.getSelection().removeAllRanges();
    S.selSource = "gutter";
    const n = +no.textContent, side = no.dataset.side;
    if (e.shiftKey && S.sel && S.sel.side === side && S.sel.path === S.active) setSel({ ...S.sel, b: n });
    else { S.dragging = true; setSel({ path: S.active, side, a: n, b: n }); }
  });
  code.addEventListener("mouseover", (e) => {
    if (!S.dragging) return;
    const no = e.target.closest(".no");
    if (no && no.dataset.side === S.sel.side && no.textContent.trim()) setSel({ ...S.sel, b: +no.textContent });
  });
  document.addEventListener("mouseup", () => { if (S.dragging) { S.dragging = false; setSel(S.sel); } });
  let selTimer = null;
  document.addEventListener("selectionchange", () => {
    clearTimeout(selTimer);
    selTimer = setTimeout(() => {
      if (S.dragging || S.selSource === "gutter" || document.activeElement?.closest?.(".inline-edit")) return;
      const ws = window.getSelection();
      const inCode = ws.rangeCount && code.contains(ws.anchorNode) && code.contains(ws.focusNode);
      // a click back in the code unhighlights; moving focus to the composer keeps the selection (rows stay lit)
      if (ws.isCollapsed) { if (inCode && S.selSource === "text") setSel(null); return; }
      if (!inCode) return;
      const lineOf = (node) => {
        const el = (node.nodeType === 1 ? node : node.parentElement).closest(".row, .half");
        if (!el) return null;
        if (el.classList.contains("half")) {
          const no = el.querySelector(".no");
          return no && no.textContent.trim() ? { n: +no.textContent, side: no.dataset.side } : null;
        }
        if (el.dataset.n) return { n: +el.dataset.n, side: "new" };
        if (el.dataset.o) return { n: +el.dataset.o, side: "old" };
        return null;
      };
      const a = lineOf(ws.anchorNode), b = lineOf(ws.focusNode);
      if (!a || !b) return;
      const side = a.side === b.side ? a.side : "new";
      S.selSource = "text";
      setSel({ path: S.active, side, a: a.n, b: b.n });
    }, 90);
  });
  code.addEventListener("scroll", () => { if (!$("#selbar").hidden && S.sel) setSel(S.sel); }, { passive: true });

  $("#selbar").addEventListener("click", (e) => {
    const act = e.target.closest("[data-act]")?.dataset.act;
    if (act === "clear") return setSel(null);
    if (act === "analyze") analyzeSelection();
    if (act === "flag") openPop();
  });

  // composer
  $("#anchors").addEventListener("click", (e) => {
    const b = e.target.closest("[data-drop]");
    if (!b) return;
    e.stopPropagation();
    if (b.dataset.drop === "live") { window.getSelection().removeAllRanges(); setSel(null); }
    else { S.anchors.splice(+b.dataset.drop, 1); renderAnchors(); }
  });
  $("#models").addEventListener("click", (e) => { const b = e.target.closest("[data-model]"); if (b) { S.tierTouched = true; S.tier = { ...S.tier, model: b.dataset.model }; renderTier(); } });
  $("#efforts").addEventListener("click", (e) => { const b = e.target.closest("[data-effort]"); if (b) { S.tierTouched = true; S.tier = { ...S.tier, effort: b.dataset.effort }; renderTier(); } });
  const input = $("#input");
  input.addEventListener("keydown", (e) => {
    const menu = $("#slash");
    if (!menu.hidden) {
      const n = menu.children.length;
      if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); S.slashIdx = (S.slashIdx + (e.key === "ArrowDown" ? 1 : n - 1)) % n; renderSlash(); return; }
      if ((e.key === "Enter" || e.key === "Tab") && n) { e.preventDefault(); pickSlash(menu.children[S.slashIdx].dataset.name); return; }
      if (e.key === "Escape") { menu.hidden = true; return; }
    }
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("#composer").requestSubmit(); }
  });
  input.addEventListener("input", () => {
    input.style.height = "auto"; input.style.height = Math.min(220, input.scrollHeight + 2) + "px";
    S.slashIdx = 0; renderSlash();
  });
  $("#slash").addEventListener("mousedown", (e) => { const li = e.target.closest("[data-name]"); if (li) { e.preventDefault(); pickSlash(li.dataset.name); } });
  $("#composer").addEventListener("submit", async (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text) return;
    const p = S.state.agent || {};
    try {
      await post("message", { text, anchors: outgoingAnchors(), ...S.tier });
      input.value = ""; input.style.height = ""; S.anchors = []; $("#slash").hidden = true;
      window.getSelection().removeAllRanges(); setSel(null); renderAnchors();
      if (p.live && !p.main && (p.model !== S.tier.model || p.effort !== S.tier.effort)) toast(`Handing over to ${tierLabel(S.tier)}`);
      refresh();
    } catch (err) { toast(err.message, true); }
  });

  // side panes and backlog
  $(".side-tabs").addEventListener("click", (e) => { const b = e.target.closest("[data-pane]"); if (b) setPane(b.dataset.pane); });
  $("#items").addEventListener("change", (e) => {
    const c = e.target.closest("[data-pick]");
    if (!c) return;
    c.checked ? S.picked.add(c.dataset.pick) : S.picked.delete(c.dataset.pick);
    syncExecSel();
  });
  $("#items").addEventListener("click", async (e) => {
    const d = e.target.closest("[data-dismiss], [data-reopen]");
    if (!d) return;
    try { await post("backlog", { id: d.dataset.dismiss || d.dataset.reopen, status: d.dataset.dismiss ? "dismissed" : "open" }); refresh(); }
    catch (err) { toast(err.message, true); }
  });
  const execute = async (ids) => {
    try {
      const r = await post("execute", { ids });
      S.picked.clear();
      syncExecSel();
      toast(`Execute requested for ${r.ids.join(", ")}`);  // the delivery line says whether it reached anyone
      refresh();
    } catch (err) { toast(err.message, true); }
  };
  $("#exec-sel").addEventListener("click", () => {
    if (S.picked.size) return execute([...S.picked]);
    toast("Tick the items to execute first, or press Execute all open");
    const boxes = $$("#items input[data-pick]");
    boxes.forEach((b) => { b.classList.remove("nudge"); void b.offsetWidth; b.classList.add("nudge"); });  // restart the flash
    boxes[0]?.focus();
  });
  $("#delivery").addEventListener("click", (e) => {
    const b = e.target.closest("[data-copy]");
    if (b) navigator.clipboard.writeText(b.dataset.copy).then(() => toast("Copied: paste it into your agent"), () => toast("Copy failed", true));
  });
  $("#exec-all").addEventListener("click", () => execute([]));
  $("#end").addEventListener("click", async (e) => {
    const b = e.currentTarget;
    if (b.dataset.armed !== "1") {
      b.dataset.armed = "1"; b.textContent = "Click again to end";
      setTimeout(() => { b.dataset.armed = ""; b.textContent = "End review"; }, 3000);
      return;
    }
    try { await post("end", {}); toast("Review ended: the backlog goes to the main agent"); refresh(); } catch (err) { toast(err.message, true); }
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { closePop(); setSel(null); }
  });
  wireStory();
}

// -------------------------------------------------------------- find
// Find in the open file: matches are CSS Custom Highlight ranges over the painted text, so they
// cross highlight.js spans without touching the DOM. It searches what the pane shows (the diff's
// rows, or the whole file), skipping hunk headers.
const FIND_MAX = 10000;
const F = { open: false, q: "", case: false, word: false, regex: false, hits: [], cur: -1, error: "" };
const canHighlight = () => typeof Highlight === "function" && CSS.highlights;

function findPattern() {
  if (!F.q) return null;
  let src = F.regex ? F.q : F.q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  if (F.word) src = `(?<![\\w$])(?:${src})(?![\\w$])`;
  try { F.error = ""; return new RegExp(src, F.case ? "g" : "gi"); }
  catch (err) { F.error = "invalid regex"; return null; }
}

function findCells() {
  return $$("#code .tx").filter((el) => !el.closest(".hunk, .inline-edit") && !el.parentElement.classList.contains("hunk"));
}

function runFind(keep) {
  const prev = F.hits[F.cur];
  F.hits = [];
  F.cur = -1;
  const re = findPattern();
  const box = $("#code");
  if (re && isCode(S.active) && !box.hidden) {
    for (const el of findCells()) {
      const text = el.textContent;
      re.lastIndex = 0;
      let m;
      while ((m = re.exec(text))) {
        if (!m[0].length) { re.lastIndex++; continue; }
        F.hits.push({ el, start: m.index, end: m.index + m[0].length });
        if (F.hits.length >= FIND_MAX) break;
      }
      if (F.hits.length >= FIND_MAX) break;
    }
  }
  if (F.hits.length) {
    // stay on the same match when the text re-renders; otherwise start at the first match in view
    const same = keep && prev && F.hits.findIndex((h) => h.el === prev.el && h.start === prev.start);
    if (same >= 0 && same !== false) F.cur = same;
    else {
      const top = box.getBoundingClientRect().top;
      F.cur = Math.max(0, F.hits.findIndex((h) => h.el.getBoundingClientRect().bottom > top + 4));
    }
  }
  paintFind(!keep);
}

function rangeOf(hit) {
  const walk = document.createTreeWalker(hit.el, NodeFilter.SHOW_TEXT);
  const r = document.createRange();
  let pos = 0, node, started = false;
  while ((node = walk.nextNode())) {
    const len = node.data.length;
    if (!started && hit.start < pos + len) { r.setStart(node, hit.start - pos); started = true; }
    if (started && hit.end <= pos + len) { r.setEnd(node, hit.end - pos); return r; }
    pos += len;
  }
  return null;
}

function paintFind(scroll) {
  const n = F.hits.length;
  $("#find-count").textContent = F.error || (!F.q ? "" : n ? `${F.cur + 1} of ${n >= FIND_MAX ? FIND_MAX + "+" : n}` : "no results");
  $("#find").classList.toggle("none", !!F.q && !n);
  $$("#find [data-find=prev], #find [data-find=next]").forEach((b) => { b.disabled = n < 2 && !(n === 1 && F.cur < 0); });
  if (canHighlight()) {
    CSS.highlights.delete("find");
    CSS.highlights.delete("find-cur");
    if (n) {
      const all = new Highlight();
      F.hits.forEach((h, i) => { if (i !== F.cur) { const r = rangeOf(h); if (r) all.add(r); } });
      CSS.highlights.set("find", all);
      const cur = rangeOf(F.hits[F.cur]);
      if (cur) CSS.highlights.set("find-cur", new Highlight(cur));
    }
  }
  $$("#code .find-row").forEach((el) => el.classList.remove("find-row"));
  if (n) F.hits[F.cur].el.parentElement.classList.add("find-row");
  renderRuler();
  if (scroll && n) revealHit(F.hits[F.cur]);
}

// Bring the current match into view, clear of the find bar.
function revealHit(hit) {
  const box = $("#code"), r = rangeOf(hit);
  if (!r) return;
  const b = box.getBoundingClientRect(), m = r.getBoundingClientRect();
  const top = b.top + 56, bottom = b.bottom - Math.min(120, b.height / 4);
  if (m.top < top || m.bottom > bottom) box.scrollTop += m.top - (b.top + b.height / 2);
  const left = b.left + 120, right = b.right - 32;
  if (m.left < left || m.right > right) box.scrollLeft += m.left - (b.left + b.width / 2);
}

// Match ticks along the right edge, so a long file shows where its matches cluster.
function renderRuler() {
  const ruler = $("#ruler"), box = $("#code");
  const on = F.open && F.hits.length && !box.hidden;
  ruler.hidden = !on;
  if (!on) return;
  ruler.style.right = `${box.offsetWidth - box.clientWidth}px`;  // beside the pane's own scrollbar, not over it
  ruler.style.bottom = `${box.offsetHeight - box.clientHeight}px`;
  const h = box.scrollHeight || 1, seen = new Set(), ticks = [];
  F.hits.forEach((hit, i) => {
    const row = hit.el.closest(".row, .split-row");
    const y = Math.round(((row ? row.offsetTop : 0) / h) * 1000) / 10;
    if (i === F.cur) ticks.push(`<i class="cur" style="top:${y}%"></i>`);
    else if (!seen.has(y)) { seen.add(y); ticks.push(`<i style="top:${y}%"></i>`); }
  });
  ruler.innerHTML = ticks.join("");
}

function stepFind(by) {
  if (!F.hits.length) return;
  F.cur = (F.cur + by + F.hits.length) % F.hits.length;
  paintFind(true);
}

function openFind() {
  if (!isCode(S.active)) return false;  // a page or the Story: the browser's own find searches it
  const ws = window.getSelection(), q = ws && !ws.isCollapsed && $("#code").contains(ws.anchorNode) ? ws.toString() : "";
  if (q && !q.includes("\n") && q.length <= 200) { F.q = q; $("#find-q").value = F.regex ? q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") : q; F.q = $("#find-q").value; }
  F.open = true;
  $("#find").hidden = false;
  const input = $("#find-q");
  input.focus();
  input.select();
  runFind(false);
  return true;
}

function closeFind() {
  F.open = false;
  $("#find").hidden = true;
  F.hits = [];
  if (canHighlight()) { CSS.highlights.delete("find"); CSS.highlights.delete("find-cur"); }
  $$("#code .find-row").forEach((el) => el.classList.remove("find-row"));
  renderRuler();
}

// renderCode calls this after it repaints, so matches follow tab, view and layout changes.
function refreshFind() {
  const code = isCode(S.active);
  $("#find").hidden = !F.open || !code;
  if (F.open && code) runFind(false);
  else renderRuler();
}

function wireFind() {
  const input = $("#find-q");
  input.addEventListener("input", () => { F.q = input.value; runFind(false); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); stepFind(e.shiftKey ? -1 : 1); }
    else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closeFind(); $("#code").focus({ preventScroll: true }); }
    else if (e.altKey && ["KeyC", "KeyW", "KeyR"].includes(e.code)) {
      e.preventDefault();
      toggleOpt({ KeyC: "case", KeyW: "word", KeyR: "regex" }[e.code]);
    }
  });
  const toggleOpt = (k) => {
    F[k] = !F[k];
    $(`#find [data-find=${k}]`).setAttribute("aria-pressed", String(F[k]));
    runFind(false);
  };
  $("#find").addEventListener("mousedown", (e) => { if (e.target.closest("button")) e.preventDefault(); });  // keep focus in the input
  $("#find").addEventListener("click", (e) => {
    const b = e.target.closest("[data-find]");
    if (!b || b.disabled) return;
    const k = b.dataset.find;
    if (k === "close") closeFind();
    else if (k === "prev" || k === "next") stepFind(k === "prev" ? -1 : 1);
    else toggleOpt(k);
  });
  document.addEventListener("keydown", (e) => {
    const mod = e.metaKey || e.ctrlKey;
    const typing = e.target.closest?.("textarea, input:not(#find-q), [contenteditable]");
    if (mod && !e.altKey && !e.shiftKey && e.key.toLowerCase() === "f" && !typing) {
      if (openFind()) e.preventDefault();  // on the Story tab the browser's own find searches the page
    } else if (F.open && ((mod && e.key.toLowerCase() === "g") || e.key === "F3")) {
      e.preventDefault();
      stepFind(e.shiftKey ? -1 : 1);
    }
  });
}

// ------------------------------------------------------------ search (Cmd+Shift+F)
// The biggrep extension's search, over the change set or the whole repository: the server streams
// ripgrep matches as NDJSON; rows are highlight.js-colored per line and the matches are CSS highlights,
// so they cross token spans without touching the DOM. Keyboard first; the mouse works too.
const SR = { open: false, q: "", case: false, word: false, regex: false, scope: "changed", paths: "", seq: 0, ctl: null,
             timer: null, slow: null, hl: null, back: null, fresh: false };
const SEARCH_KEY = "review-desk.search";
try { Object.assign(SR, JSON.parse(localStorage.getItem(SEARCH_KEY)) || {}); } catch (_) {}
const saveSearch = () => { try { localStorage.setItem(SEARCH_KEY, JSON.stringify({ q: SR.q, case: SR.case, word: SR.word, regex: SR.regex, scope: SR.scope, paths: SR.paths })); } catch (_) {} };

function openSearch() {
  const ws = window.getSelection(), sel = ws && !ws.isCollapsed && $("#code").contains(ws.anchorNode) ? ws.toString() : "";
  if (sel && !sel.includes("\n") && sel.length <= 200) SR.q = SR.regex ? sel.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") : sel;
  if (!SR.open) SR.back = document.activeElement;
  SR.open = true;
  $("#search").hidden = false;
  $("#s-q").value = SR.q;
  $("#s-paths").value = SR.paths;
  searchOpts();
  $("#s-q").focus();
  $("#s-q").select();
  if (SR.q && !$("#s-results").children.length) queueSearch(true);
}

function closeSearch(restore = true) {
  if (!SR.open) return;
  SR.open = false;
  $("#search").hidden = true;
  if (SR.ctl) SR.ctl.abort();
  clearTimeout(SR.timer);
  if (canHighlight()) { CSS.highlights.delete("search"); CSS.highlights.delete("search-cur"); }
  if (restore && SR.back && document.contains(SR.back)) SR.back.focus({ preventScroll: true });
  // keep the rows, so reopening shows the last results at once; repaint their highlights then
  SR.hl = null;
}

function searchOpts() {
  ["case", "word", "regex"].forEach((k) => $(`#search [data-sopt=${k}]`).setAttribute("aria-pressed", String(SR[k])));
  $$("#search [data-scope]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.scope === SR.scope)));
  const n = (S.all.files || []).filter((f) => f.badge !== "D").length;
  $("#s-q").placeholder = SR.scope === "repo" ? "Search the repository" : `Search ${n} changed file${n === 1 ? "" : "s"}`;
}

function queueSearch(now) {
  clearTimeout(SR.timer);
  saveSearch();
  SR.timer = setTimeout(runSearch, now ? 0 : 140);
}

async function runSearch() {
  SR.timer = null;
  if (SR.ctl) SR.ctl.abort();
  const seq = ++SR.seq, ctl = new AbortController();
  SR.ctl = ctl;
  clearTimeout(SR.slow);
  const status = $("#s-status");
  if (!SR.q) {
    $("#s-results").innerHTML = "";
    status.className = "s-status";
    status.textContent = "";
    return;
  }
  SR.slow = setTimeout(() => { if (seq === SR.seq) { status.className = "s-status busy"; status.textContent = "searching…"; } }, 150);
  SR.fresh = true;  // the old rows stay until the first new record arrives, so fast typing never flashes empty
  const params = new URLSearchParams({ q: SR.q, regex: +SR.regex, case: +SR.case, word: +SR.word, scope: SR.scope, paths: SR.paths });
  try {
    const r = await fetch(`/api/${D.sid}/search?${params}`, { headers: { "X-Desk-Token": D.token }, signal: ctl.signal });
    if (!r.ok) throw new Error(`search failed (HTTP ${r.status})`);
    const reader = r.body.getReader(), dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done || seq !== SR.seq) break;
      buf += dec.decode(value, { stream: true });
      const lines = buf.split("\n");
      buf = lines.pop();
      const recs = lines.filter(Boolean).map((l) => JSON.parse(l));
      if (recs.length) searchRecords(recs, seq);
    }
  } catch (err) {
    if (err.name !== "AbortError" && seq === SR.seq) { status.className = "s-status err"; status.textContent = err.message; }
  } finally {
    if (seq === SR.seq) clearTimeout(SR.slow);
  }
}

function searchRecords(recs, seq) {
  if (seq !== SR.seq) return;
  const box = $("#s-results"), status = $("#s-status");
  if (SR.fresh) {
    SR.fresh = false;
    box.innerHTML = "";
    box.scrollTop = 0;
    SR.hl = null;
  }
  if (canHighlight() && !SR.hl) { SR.hl = new Highlight(); CSS.highlights.set("search", SR.hl); CSS.highlights.delete("search-cur"); }
  for (const rec of recs) {
    if (rec.t === "m") { searchRow(box, rec); continue; }
    clearTimeout(SR.slow);
    if (rec.t === "error") {
      box.innerHTML = "";
      status.className = "s-status err";
      status.textContent = rec.msg;
      continue;
    }
    status.className = "s-status";
    const where = SR.scope === "repo" ? "in the repository" : "in changed files";
    status.innerHTML = rec.matches
      ? `<span class="count">${rec.matches}${rec.truncated ? "+" : ""} result${rec.matches === 1 ? "" : "s"}</span> · ${rec.files} file${rec.files === 1 ? "" : "s"} · ${rec.ms} ms` +
        (rec.truncated ? ' · <span class="warn">capped, narrow the query or files</span>' : "") + '<span class="grow"></span><span class="keys">↵ open · space preview · esc</span>'
      : `No results ${where}`;
    if (!rec.matches) box.innerHTML = "";
    if (!$(".s-item.selected", box)) { const first = $(".s-hit", box); if (first) selectHit(first, false); }
  }
}

function searchRow(box, rec) {
  let g = box.lastElementChild;
  if (!g || g.dataset.path !== rec.path) {
    const slash = rec.path.lastIndexOf("/");
    g = document.createElement("div");
    g.className = "s-group";
    g.dataset.path = rec.path;
    g.innerHTML = `<div class="s-item s-file" role="option" data-path="${esc(rec.path)}"><span class="chev" aria-hidden="true"></span>` +
      (rec.badge ? `<span class="st" data-s="${esc(rec.badge)}">${esc(rec.badge)}</span>` : "") +
      `<span class="name">${esc(rec.path.slice(slash + 1))}</span><span class="dir">${esc(slash >= 0 ? rec.path.slice(0, slash) : "")}</span>` +
      '<span class="count">0</span></div><div class="s-hits"></div>';
    box.appendChild(g);
  }
  const count = $(".count", g);
  count.textContent = +count.textContent + 1;
  const row = document.createElement("div");
  row.className = "s-item s-hit";
  row.setAttribute("role", "option");
  row.dataset.path = rec.path;
  row.dataset.line = rec.line;
  // previews drop their indentation (the match ranges shift with it), so the code starts at the gutter
  const indent = rec.text.length - rec.text.trimStart().length;
  const text = rec.text.slice(indent);
  const lang = langFor(rec.path);
  let code = esc(text) || "​";
  if (window.hljs && lang && hljs.getLanguage(lang)) {
    try { code = hljs.highlight(text, { language: lang, ignoreIllegals: true }).value || "​"; } catch (_) {}
  }
  row.innerHTML = `<span class="ln">${rec.line}</span><span class="s-code">${code}</span>`;
  row._ranges = rec.ranges.map(([a, b]) => [Math.max(0, a - indent), b - indent]).filter(([a, b]) => b > a);
  $(".s-hits", g).appendChild(row);
  if (SR.hl) hitRanges(row).forEach((r) => SR.hl.add(r));
}

const hitRanges = (row) => (row._ranges || []).map(([a, b]) => rangeOf({ el: $(".s-code", row), start: a, end: b })).filter(Boolean);

// rows the keyboard walks: every file header, and the matches of expanded files
const searchItems = () => $$("#s-results .s-item").filter((el) => !el.closest(".s-group.folded .s-hits"));

function selectHit(el, scroll = true) {
  $$("#s-results .s-item.selected").forEach((x) => x.classList.remove("selected"));
  el.classList.add("selected");
  if (scroll) el.scrollIntoView({ block: "nearest" });
  if (canHighlight()) {
    const cur = new Highlight(...(el.classList.contains("s-hit") ? hitRanges(el) : []));
    CSS.highlights.set("search-cur", cur);
  }
}

function stepHit(by) {
  const items = searchItems(), cur = items.findIndex((x) => x.classList.contains("selected"));
  if (!items.length) return;
  if (cur <= 0 && by < 0) { $("#s-q").focus(); return; }
  selectHit(items[Math.max(0, Math.min(items.length - 1, cur + by))]);
}

function foldGroup(g, fold) {
  g.classList.toggle("folded", fold);
  if (fold) { const head = $(".s-file", g); if (!head.classList.contains("selected") && $(".selected", g)) selectHit(head); }
}

// Open a match at its line, carry the query into the file's find bar, and (unless previewing) close.
async function openHit(el, preview) {
  if (!el) return;
  const hit = el.classList.contains("s-hit") ? el : $(".s-hit", el.closest(".s-group"));
  if (!hit) return;
  const path = hit.dataset.path, line = +hit.dataset.line;
  if (!preview) closeSearch(false);
  await openRef({ path, range: String(line) });  // search reads the working tree
  if (S.active !== path) return;
  Object.assign(F, { q: SR.q, case: SR.case, word: SR.word, regex: SR.regex, open: true });
  ["case", "word", "regex"].forEach((k) => $(`#find [data-find=${k}]`).setAttribute("aria-pressed", String(F[k])));
  $("#find-q").value = SR.q;
  $("#find").hidden = false;
  runFind(true);
  const at = F.hits.findIndex((h) => +((h.el.closest(".row, .half") || {}).dataset?.n || (h.el.parentElement.querySelector(".no") || {}).textContent) === line);
  if (at >= 0) { F.cur = at; paintFind(false); }
  if (preview) { $("#s-results").focus({ preventScroll: true }); if (SR.open) { SR.hl = null; repaintSearch(); } }
}

// CSS highlights are global; the file's find bar replaced ours while previewing, so put ours back
function repaintSearch() {
  if (!canHighlight() || !SR.open) return;
  SR.hl = new Highlight();
  $$("#s-results .s-hit").forEach((row) => hitRanges(row).forEach((r) => SR.hl.add(r)));
  CSS.highlights.set("search", SR.hl);
  const sel = $("#s-results .s-item.selected");
  if (sel) selectHit(sel, false);
}

function wireSearch() {
  const q = $("#s-q"), paths = $("#s-paths"), box = $("#s-results"), panel = $("#search");
  q.addEventListener("input", () => { SR.q = q.value; queueSearch(false); });
  paths.addEventListener("input", () => { SR.paths = paths.value; queueSearch(false); });
  const toggle = (k) => {
    if (k === "scope") SR.scope = SR.scope === "repo" ? "changed" : "repo"; else SR[k] = !SR[k];
    searchOpts();
    queueSearch(true);
  };
  panel.addEventListener("mousedown", (e) => { if (e.target.closest("button")) e.preventDefault(); });  // keep focus where it is
  panel.addEventListener("click", (e) => {
    const o = e.target.closest("[data-sopt]"), sc = e.target.closest("[data-scope]");
    if (o) return toggle(o.dataset.sopt);
    if (sc && sc.dataset.scope !== SR.scope) return toggle("scope");
    const item = e.target.closest(".s-item");
    if (!item) return;
    if (item.classList.contains("s-file") && !e.target.closest(".name")) {
      const g = item.closest(".s-group");
      foldGroup(g, !g.classList.contains("folded"));
      return selectHit(item);
    }
    selectHit(item);
    openHit(item, e.detail === 1 && e.metaKey);  // click opens; Cmd+click previews
  });
  panel.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); return closeSearch(); }
    if (e.altKey && !e.metaKey && !e.ctrlKey && ["KeyC", "KeyW", "KeyR", "KeyS"].includes(e.code)) {
      e.preventDefault();
      return toggle({ KeyC: "case", KeyW: "word", KeyR: "regex", KeyS: "scope" }[e.code]);
    }
  });
  [q, paths].forEach((input) => input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      box.focus({ preventScroll: true });
      const sel = $(".s-item.selected", box) || $(".s-hit", box);
      if (sel) selectHit(sel);
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (SR.timer || SR.fresh) queueSearch(true);  // typed faster than the debounce: search now
      else openHit($(".s-item.selected", box) || $(".s-hit", box), e.metaKey || e.ctrlKey);
    }
  }));
  box.addEventListener("keydown", (e) => {
    const sel = $(".s-item.selected", box);
    const page = Math.max(1, Math.floor(box.clientHeight / 24) - 1);
    const mod = e.metaKey || e.ctrlKey;
    if (mod && e.key.toLowerCase() === "c" && sel && window.getSelection().isCollapsed) {
      e.preventDefault();
      const text = sel.classList.contains("s-hit") ? `${sel.dataset.path}:${sel.dataset.line}:${$(".s-code", sel).textContent}` : sel.dataset.path;
      navigator.clipboard.writeText(text).then(() => toast("Copied"), () => toast("Copy failed", true));
      return;
    }
    if (mod || e.altKey) return;
    const moves = { ArrowDown: 1, j: 1, ArrowUp: -1, k: -1, PageDown: page, PageUp: -page };
    if (e.key in moves) { e.preventDefault(); return stepHit(moves[e.key]); }
    if (e.key === "Home" || e.key === "End") { e.preventDefault(); const it = searchItems(); if (it.length) selectHit(it[e.key === "Home" ? 0 : it.length - 1]); return; }
    if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
      e.preventDefault();
      if (sel) foldGroup(sel.closest(".s-group"), e.key === "ArrowLeft");
      return;
    }
    if (e.key === "Enter") { e.preventDefault(); return openHit(sel, false); }
    if (e.key === " ") { e.preventDefault(); return openHit(sel, true); }
    if (e.key.length === 1) q.focus();  // typing goes back to the query; the character lands there
  });
  document.addEventListener("mousedown", (e) => { if (SR.open && !e.target.closest("#search")) closeSearch(false); });
  const hotkey = (e) => {
    if ((e.metaKey || e.ctrlKey) && e.shiftKey && !e.altKey && e.key.toLowerCase() === "f") { e.preventDefault(); e.stopPropagation(); openSearch(); }
  };
  document.addEventListener("keydown", hotkey, true);
  $("#story").addEventListener("load", () => { try { $("#story").contentDocument.addEventListener("keydown", hotkey, true); } catch (_) {} });
  window.addEventListener("message", (e) => {
    if (e.data && e.data.desk === "search" && $$("#panes .page-frame").some((f) => f.contentWindow === e.source)) openSearch();
  });
}

// ------------------------------------------------------------- tab strip
function wireTabs() {
  const strip = $("#tabs");
  strip.addEventListener("click", (e) => {
    if (S.tabDrag && S.tabDrag.moved) return;
    const c = e.target.closest("[data-close]");
    if (c) { e.stopPropagation(); return closeTab(c.dataset.close); }
    const t = e.target.closest("[data-tab]");
    if (t) activateTab(t.dataset.tab);
  });
  strip.addEventListener("auxclick", (e) => { const t = e.target.closest("[data-tab]"); if (e.button === 1 && t && t.dataset.tab !== "story") closeTab(t.dataset.tab); });
  strip.addEventListener("mousedown", (e) => { if (e.button === 1) e.preventDefault(); });  // no autoscroll cursor
  strip.addEventListener("keydown", (e) => {
    const t = e.target.closest(".tab");
    if (!t) return;
    const path = t.dataset.tab;
    if ((e.key === "Enter" || e.key === " ") && !e.altKey) { e.preventDefault(); activateTab(path); }
    else if (e.altKey && e.shiftKey && (e.key === "ArrowLeft" || e.key === "ArrowRight")) { e.preventDefault(); shiftTab(path, e.key === "ArrowLeft" ? -1 : 1); }
    else if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
      e.preventDefault();
      const all = $$(".tab", strip), i = all.indexOf(t) + (e.key === "ArrowLeft" ? -1 : 1);
      all[Math.max(0, Math.min(all.length - 1, i))].focus();
    } else if ((e.key === "Delete" || e.key === "Backspace") && path !== "story") { e.preventDefault(); closeTab(path); $(".tab.active", strip)?.focus(); }
  });
  // a vertical wheel scrolls the strip sideways; the strip itself never scrolls vertically
  strip.addEventListener("wheel", (e) => {
    if (Math.abs(e.deltaY) <= Math.abs(e.deltaX)) return;
    e.preventDefault();
    strip.scrollLeft += e.deltaY * (e.deltaMode === 1 ? 16 : 1);
  }, { passive: false });
  strip.addEventListener("scroll", tabOverflow, { passive: true });
  new ResizeObserver(() => { revealTab(); tabOverflow(); }).observe(strip);

  // drag a file tab sideways to reorder; the others slide out of the way (Story stays first)
  strip.addEventListener("pointerdown", (e) => {
    const t = e.target.closest(".tab");
    if (e.button !== 0 || !t || t.dataset.tab === "story" || e.target.closest(".close")) return;
    S.tabDrag = { el: t, path: t.dataset.tab, x0: e.clientX, grab: e.clientX - t.getBoundingClientRect().left, moved: false, id: e.pointerId };
  });
  const onMove = (e) => {
    const d = S.tabDrag;
    if (!d || e.pointerId !== d.id) return;
    if (!d.moved) {
      if (Math.abs(e.clientX - d.x0) < 5) return;
      d.moved = true;
      d.el.setPointerCapture(d.id);
      d.el.classList.add("dragging");
      strip.classList.add("reordering");
      document.body.classList.add("tab-dragging");
    }
    // auto-scroll near the edges, then slot the tab where its centre now is
    const box = strip.getBoundingClientRect(), edge = 36;
    if (e.clientX < box.left + edge) strip.scrollLeft -= 12;
    else if (e.clientX > box.right - edge) strip.scrollLeft += 12;
    const centre = e.clientX - d.grab + d.el.offsetWidth / 2;
    const others = $$(".tab", strip).filter((x) => x !== d.el);
    let at = others.findIndex((x) => { const r = x.getBoundingClientRect(); return centre < r.left + r.width / 2; });
    if (at < 0) at = others.length;
    at = Math.max(1, at);
    if (others[at] !== d.el.nextElementSibling || (at === others.length && d.el.nextElementSibling)) {
      const before = new Map(others.map((x) => [x, x.getBoundingClientRect().left]));
      strip.insertBefore(d.el, others[at] || null);
      others.forEach((x) => {  // FLIP: slide each displaced tab from where it was
        const dx = before.get(x) - x.getBoundingClientRect().left;
        if (!dx) return;
        x.style.transition = "none";
        x.style.transform = `translateX(${dx}px)`;
        requestAnimationFrame(() => { x.style.transition = "transform 0.14s ease"; x.style.transform = ""; });
      });
    }
    const home = d.el.getBoundingClientRect().left - (parseFloat(d.el.style.translate) || 0);
    d.el.style.translate = `${e.clientX - d.grab - home}px 0`;
  };
  const onUp = (e) => {
    const d = S.tabDrag;
    if (!d || e.pointerId !== d.id) return;
    if (d.moved) {
      // the strip's order for tabs still open, then any a refresh opened during the drag
      const order = [...new Set($$(".tab", strip).map((x) => x.dataset.tab))].filter((t) => S.tabs.includes(t));
      S.tabs = [...order, ...S.tabs.filter((t) => !order.includes(t))];
      strip.classList.remove("reordering");
      document.body.classList.remove("tab-dragging");
      setTimeout(() => { S.tabDrag = null; renderTabs(); });  // after the click this pointerup fires
    } else S.tabDrag = null;
  };
  strip.addEventListener("pointermove", onMove);
  strip.addEventListener("pointerup", onUp);
  strip.addEventListener("pointercancel", onUp);

  const more = $("#tab-more"), menu = $("#tab-menu");
  more.addEventListener("click", (e) => { e.stopPropagation(); tabMenu(menu.hidden); });
  menu.addEventListener("click", (e) => {
    const c = e.target.closest("[data-close]");
    if (c) { e.stopPropagation(); closeTab(c.dataset.close); return S.tabs.length > 1 ? renderTabMenu() : tabMenu(false); }
    const t = e.target.closest("[data-tab]");
    if (t) { tabMenu(false); activateTab(t.dataset.tab); }
  });
  document.addEventListener("click", (e) => { if (!menu.hidden && !e.target.closest("#tab-menu, #tab-more")) tabMenu(false); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !menu.hidden) { tabMenu(false); more.focus(); } });
}

// ------------------------------------------------------------- pane layout
// Widths and the files panel's open state are a per-browser convenience (localStorage; may be unavailable).
const LAYOUT_KEY = "review-desk:layout";
const LIMITS = { rail: [170, 520], side: [300, 1100] };
// Default split: chat : editor = 1 : φ, so the chat takes 1/φ² (≈ 38.2%) of the width the two share.
const PHI = (1 + Math.sqrt(5)) / 2;
const MIN_EDITOR = 380;
function loadLayout() { try { return JSON.parse(localStorage.getItem(LAYOUT_KEY)) || {}; } catch (_) { return {}; } }
function saveLayout(l) { try { localStorage.setItem(LAYOUT_KEY, JSON.stringify(l)); } catch (_) {} }

function applyLayout(l) {
  const desk = $("#desk");
  if (l.rail) desk.style.setProperty("--rail-w", `${l.rail}px`);
  if (l.side) desk.style.setProperty("--side-w", `${l.side}px`);
  const closed = !!l.railClosed;
  desk.classList.toggle("rail-closed", closed);
  const t = $("#rail-toggle");
  t.setAttribute("aria-expanded", String(!closed));
  t.title = `${closed ? "Show" : "Hide"} files (Ctrl/Cmd+B)`;
}

function paneWidth(which) { return Math.round((which === "rail" ? $("#rail") : $("#side")).getBoundingClientRect().width); }

function setWidth(which, px) {
  // the editor keeps at least MIN_EDITOR px; the other side panel keeps its current width
  const desk = $("#desk");
  const closed = desk.classList.contains("rail-closed");
  const inner = desk.clientWidth - 20 - (closed ? 10 : 20);  // padding and the visible handles
  const other = which === "rail" ? paneWidth("side") : (closed ? 0 : paneWidth("rail"));
  const [lo, hi] = LIMITS[which];
  const w = Math.round(Math.max(lo, Math.min(hi, inner - other - MIN_EDITOR, px)));
  desk.style.setProperty(which === "rail" ? "--rail-w" : "--side-w", `${w}px`);
  return w;
}

function goldenSide(layout) {
  // only while the chat width is not one the user dragged; desktop layout only
  if (layout.side || window.innerWidth <= 900) return;
  const desk = $("#desk");
  const closed = desk.classList.contains("rail-closed");
  const shared = desk.clientWidth - 20 - (closed ? 10 : 20) - (closed ? 0 : paneWidth("rail"));
  setWidth("side", shared / (1 + PHI));
}

// Saved widths come from whatever window they were dragged in. Restored into a narrower one they could take
// the whole width and leave the editor (story, pages, code) at 0 px, an empty desk in that browser profile
// only. Fit them on load and on every resize; the saved values stay, so a wider window gets them back.
function fitLayout(layout) {
  if (window.innerWidth <= 900) return;  // the narrow layout stacks the panels
  if (layout.side) setWidth("side", layout.side);
  if (layout.rail) setWidth("rail", layout.rail);
  if (layout.side) setWidth("side", layout.side);  // again, now against the fitted files panel
  goldenSide(layout);
}

function wireLayout() {
  const code = $("#code");
  new ResizeObserver(() => code.style.setProperty("--code-w", `${code.clientWidth}px`)).observe(code);
  const layout = loadLayout();
  applyLayout(layout);
  const desk = $("#desk");
  fitLayout(layout);
  let fitTimer = 0;
  window.addEventListener("resize", () => { clearTimeout(fitTimer); fitTimer = setTimeout(() => fitLayout(layout), 60); });
  $$(".split").forEach((h) => {
    const which = h.dataset.split;
    h.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      e.preventDefault();
      try { h.setPointerCapture(e.pointerId); } catch (_) {}
      h.classList.add("active");
      document.body.classList.add("resizing");
      hideSel();
      const box = desk.getBoundingClientRect();
      const move = (ev) => {
        const px = which === "rail" ? ev.clientX - box.left - 10 - 5 : box.right - 10 - 5 - ev.clientX;
        layout[which] = setWidth(which, px);
        if (which === "rail") goldenSide(layout);  // an undragged chat keeps the golden split
      };
      const up = () => {
        h.classList.remove("active");
        document.body.classList.remove("resizing");
        window.removeEventListener("pointermove", move);
        saveLayout(layout);
        if (S.sel) setSel(S.sel);  // re-place the selection bar against the new geometry
      };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up, { once: true });
      window.addEventListener("pointercancel", up, { once: true });
    });
    // keyboard: arrows nudge by 24px, double-click or Enter resets to the default width
    h.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight" && e.key !== "Enter") return;
      e.preventDefault();
      if (e.key === "Enter") { delete layout[which]; desk.style.removeProperty(which === "rail" ? "--rail-w" : "--side-w"); goldenSide(layout); }
      else {
        const grow = (e.key === "ArrowRight") === (which === "rail");
        layout[which] = setWidth(which, paneWidth(which) + (grow ? 24 : -24));
      }
      saveLayout(layout);
    });
    h.addEventListener("dblclick", () => {
      delete layout[which];
      desk.style.removeProperty(which === "rail" ? "--rail-w" : "--side-w");
      goldenSide(layout);
      saveLayout(layout);
    });
  });
  const toggle = () => {
    layout.railClosed = !layout.railClosed;
    applyLayout(layout);
    goldenSide(layout);
    saveLayout(layout);
    if (S.sel) setSel(S.sel);
  };
  $("#rail-toggle").addEventListener("click", toggle);
  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === "b") { e.preventDefault(); toggle(); }
  });
  // a window resize must never squeeze the editor below its minimum
  window.addEventListener("resize", () => {
    if (window.innerWidth <= 900) return;
    ["side", "rail"].forEach((w) => { if (layout[w]) layout[w] = setWidth(w, layout[w]); });
    goldenSide(layout);
  });
}

function setPane(p) {
  S.pane = p;
  $$(".side-tabs [data-pane]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.pane === p)));
  $("#chat-pane").hidden = p !== "chat";
  $("#backlog-pane").hidden = p !== "backlog";
}

// A browser keeps at most 6 connections per host for each profile, and a streaming tab holds one, so a 7th
// desk tab would never load. A hidden tab closes its stream and checks in once a minute instead (the server
// still counts it as open); showing the tab again reconnects and redraws whatever changed meanwhile.
const TAB_ID = Math.random().toString(36).slice(2, 12);
const live = { es: null, checkIns: null };
function connect() {
  if (live.es) return;
  const es = live.es = new EventSource(`/api/${D.sid}/events?tab=${TAB_ID}&t=${encodeURIComponent(D.token)}`);
  es.addEventListener("sync", refresh);
  es.addEventListener("stream", (ev) => {
    try { const d = JSON.parse(ev.data); S.draft = d && d.reply_to ? d : null; } catch (_) { S.draft = null; }
    renderDraft();
  });
  es.onerror = () => {
    if (es !== live.es) return;  // a stream this tab closed on purpose
    S.offline = true; const el = $("#presence"); el.dataset.state = "offline"; $(".label", el).textContent = "desk server offline";
  };
  es.onopen = () => { if (S.offline) { S.offline = false; recoverFromOutage(); } };
}
const checkIn = () => api(`seen?tab=${TAB_ID}`).catch(() => {});
function park() {
  if (live.es) { live.es.close(); live.es = null; }
  if (!live.checkIns) { checkIn(); live.checkIns = setInterval(checkIn, 60000); }
}
function resume() {
  clearInterval(live.checkIns); live.checkIns = null;
  if (live.es) return;
  connect();
  refresh().then(() => { if (S.active !== "story" && !isPage(S.active)) renderCode(); });
}
function wireVisibility() {
  document.addEventListener("visibilitychange", () => (document.hidden ? park() : resume()));
  window.addEventListener("pagehide", park);
  window.addEventListener("pageshow", () => { if (!document.hidden) resume(); });
  if (document.hidden) park(); else connect();
}

// The server went away (a restart after an upgrade) and came back. A frame that tried to load meanwhile shows
// the browser's error page and never retries, so reload every page frame and the story, then redraw.
function recoverFromOutage() {
  $$("#panes .page-frame").forEach((f) => { f.src = f.src; });
  const story = $("#story");
  if (story.src) story.src = story.src;
  refresh().then(() => { if (S.active !== "story" && !isPage(S.active)) renderCode(); });
}

wire();
wirePicker();
wireFind();
wireSearch();
wireLayout();
renderTabs();
api("skills").then((d) => { S.skills = d.skills || []; }).catch(() => {});
setInterval(tickWaiting, 1000);
refresh().then(() => { if (window.hljs && S.active !== "story") renderCode(); });
wireVisibility();
window.addEventListener("load", () => { if (S.active !== "story") renderCode(); S.chatSig = ""; if (S.state) renderChat(); });
