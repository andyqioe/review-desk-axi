/*
 * Glossary cards and "Ask Claude" for implementation-summary pages.
 *
 * Terms marked <span data-term="key"> open a card on hover, focus or tap: the definition the
 * author wrote, a link to where the term lives in code, an "Ask Claude" button and up to three
 * suggested questions. Selecting any text in the page offers the same button.
 *
 * Inside the Review Desk (the story tab or a page tab) a question is posted to the desk, which puts
 * it in the chat box for the user to edit and send; the page itself never sends anything. Opened on
 * its own, the page copies a ready-to-paste prompt instead.
 */
(() => {
  "use strict";

  const data = document.getElementById("glossary-data");
  let G = {};
  try { G = data ? JSON.parse(data.textContent) : {}; } catch (_) { G = {}; }
  const inDesk = window.parent !== window;
  const ICON = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M2.5 3.5h11v7h-6l-3 2.5v-2.5h-2z" stroke-linejoin="round"/></svg>';

  const el = (tag, cls, html) => { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; };
  const card = el("div", "gloss-card");
  card.setAttribute("role", "dialog");
  const selAsk = el("button", "ask-btn sel-ask", ICON + "Ask Claude");
  selAsk.type = "button";
  const toastEl = el("div", "gloss-toast");
  document.body.append(card, selAsk, toastEl);

  let current = null, hideTimer = null, toastTimer = null;

  function sectionOf(node) {
    const s = node && node.closest && node.closest("section, header");
    if (!s) return "";
    const h = s.querySelector("h1, h2");
    const i = s.querySelector(".index");
    return [i && i.textContent.trim(), h && h.textContent.trim()].filter(Boolean).join(" · ");
  }

  function toast(msg) {
    toastEl.textContent = msg;
    toastEl.classList.add("on");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toastEl.classList.remove("on"), 2600);
  }

  async function ask(quote, node, question) {
    hideNow();
    selAsk.classList.remove("on");
    const section = sectionOf(node);
    if (inDesk) {
      window.parent.postMessage({ desk: "ask", quote, section, question }, "*");  // the desk confirms it
      return;
    }
    const text = `About "${quote}"${section ? ` (${section})` : ""} in ${document.title}: ${question}`;
    try { await navigator.clipboard.writeText(text); toast("Question copied: paste it into Claude"); }
    catch (_) { window.prompt("Copy this question for Claude:", text); }
  }

  function place(box, anchor) {
    const r = anchor.getBoundingClientRect(), p = box.getBoundingClientRect();
    const x = Math.min(Math.max(16, r.left), window.innerWidth - p.width - 16);
    let y = r.bottom + 8;
    if (y + p.height > window.innerHeight - 16) y = r.top - p.height - 8;
    box.style.left = `${x}px`;
    box.style.top = `${Math.max(16, y)}px`;
  }

  function show(term) {
    const g = G[term.dataset.term];
    if (!g) return;
    clearTimeout(hideTimer);
    if (current && current !== term) current.setAttribute("aria-expanded", "false");
    current = term;
    term.setAttribute("aria-expanded", "true");
    card.replaceChildren();
    card.append(el("p", "gloss-name", ""), el("div", "gloss-def", g.def));
    card.firstChild.textContent = g.name;
    if (g.loc) {
      const a = el("a", "gloss-ref");
      // paths wrap after each "/", never mid-name and never past the card's edge
      a.append(...g.loc.split("/").flatMap((part, i, all) => (i < all.length - 1 ? [`${part}/`, document.createElement("wbr")] : [part])));
      if (g.href) a.href = g.href;
      a.title = "Open in editor";
      card.append(a);
    }
    const row = el("div", "gloss-actions");
    const b = el("button", "ask-btn", ICON + "Ask Claude");
    b.type = "button";
    b.onclick = () => ask(g.name, term, `What does "${g.name}" mean here, and why does it matter?`);
    row.append(b);
    (g.ask || []).forEach((q) => {
      const s = el("button", "ask-chip");
      s.type = "button";
      s.textContent = q;
      s.onclick = () => ask(g.name, term, q);
      row.append(s);
    });
    card.append(row);
    card.classList.add("on");
    place(card, term);
  }

  function hideSoon() { clearTimeout(hideTimer); hideTimer = setTimeout(hideNow, 160); }
  function hideNow() {
    card.classList.remove("on");
    if (current) current.setAttribute("aria-expanded", "false");
    current = null;
  }

  document.querySelectorAll("[data-term]").forEach((t) => {
    if (!G[t.dataset.term]) return;
    t.classList.add("term");
    t.tabIndex = 0;
    t.setAttribute("aria-expanded", "false");
    t.addEventListener("mouseenter", () => show(t));
    t.addEventListener("mouseleave", hideSoon);
    t.addEventListener("focus", () => show(t));
    t.addEventListener("blur", (e) => { if (!card.contains(e.relatedTarget)) hideSoon(); });
    t.addEventListener("click", () => show(t));  // touch has no hover
  });
  card.addEventListener("mouseenter", () => clearTimeout(hideTimer));
  card.addEventListener("mouseleave", hideSoon);
  card.addEventListener("focusout", (e) => { if (!card.contains(e.relatedTarget) && e.relatedTarget !== current) hideSoon(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") { hideNow(); selAsk.classList.remove("on"); } });
  window.addEventListener("scroll", () => { if (current) place(card, current); selAsk.classList.remove("on"); }, { passive: true });

  // any selection inside the page gets the same button
  let selText = "", selNode = null;
  document.addEventListener("mouseup", (e) => {
    if (e.target.closest && e.target.closest(".gloss-card, .sel-ask")) return;
    setTimeout(() => {
      const s = window.getSelection();
      const text = s && s.toString().replace(/\s+/g, " ").trim();
      const node = s && s.anchorNode && (s.anchorNode.nodeType === 1 ? s.anchorNode : s.anchorNode.parentElement);
      if (!text || !s.rangeCount || !node || !node.closest("main")) { selAsk.classList.remove("on"); return; }
      selText = text.slice(0, 600);
      selNode = node;
      hideNow();
      selAsk.classList.add("on");
      const r = s.getRangeAt(0).getBoundingClientRect();
      const w = selAsk.offsetWidth, h = selAsk.offsetHeight;
      selAsk.style.left = `${Math.min(Math.max(16, r.left + r.width / 2 - w / 2), window.innerWidth - w - 16)}px`;
      selAsk.style.top = `${r.top - h - 8 < 16 ? r.bottom + 8 : r.top - h - 8}px`;
    }, 0);
  });
  selAsk.addEventListener("click", () => ask(selText, selNode, ""));
})();
