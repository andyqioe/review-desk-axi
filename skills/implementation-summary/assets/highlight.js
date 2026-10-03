// Syntax highlighting for code figures, run lazily when a block nears the viewport.
//
// Each block is highlighted as whole text, not line by line, so a construct that spans lines
// (a docstring, a block comment, a template string) keeps its colour on every line. A diff is
// two texts: the old side (context + removed lines) and the new side (context + added lines),
// highlighted separately like a side-by-side view. Notes and hunk headers that skip lines split
// segments; a header marked `join` (nothing hidden since the previous hunk) does not.

// Split highlight.js HTML into one HTML string per line. Spans open at a newline are closed at
// the end of that line and reopened at the start of the next, so every line is well formed.
function splitLines(html) {
  const lines = [];
  const open = [];
  let cur = "";
  const re = /<span[^>]*>|<\/span>|[^<]+/g;
  let m;
  while ((m = re.exec(html))) {
    const t = m[0];
    if (t.startsWith("<span")) {
      open.push(t);
      cur += t;
    } else if (t === "</span>") {
      open.pop();
      cur += t;
    } else {
      t.split("\n").forEach((part, i) => {
        if (i > 0) {
          lines.push(cur + "</span>".repeat(open.length));
          cur = open.join("");
        }
        cur += part;
      });
    }
  }
  lines.push(cur + "</span>".repeat(open.length));
  return lines;
}

// A row whose HTML holds no text (an empty line inside a docstring comes back as an empty span)
// would collapse to zero height and shift every later line against its gutter number.
function keepHeight(line) {
  return line.replace(/<[^>]*>/g, "") === "" ? line + "\u200b" : line;
}

function highlightBlock(el, hljs) {
  if (el.dataset.hl) return;
  el.dataset.hl = "1";
  const lang = (el.className.match(/language-(\S+)/) || [])[1];
  if (!lang || !hljs.getLanguage(lang)) return;
  const text = (row) => row.textContent.replace(/\u200b/g, "");
  const paint = (rows, owns) => {
    if (!rows.length) return;
    const html = hljs.highlight(rows.map(text).join("\n"), { language: lang, ignoreIllegals: true }).value;
    splitLines(html).forEach((line, i) => {
      if (rows[i] && owns(rows[i])) rows[i].innerHTML = keepHeight(line);
    });
  };
  let seg = [];
  const flush = () => {
    const has = (r, c) => r.classList.contains(c);
    if (seg.some((r) => has(r, "del"))) paint(seg.filter((r) => !has(r, "add")), (r) => has(r, "del"));
    paint(seg.filter((r) => !has(r, "del")), () => true);
    seg = [];
  };
  el.querySelectorAll(".ln").forEach((row) => {
    const cls = row.classList;
    if (cls.contains("meta") || (cls.contains("hunk") && !cls.contains("join"))) flush();
    else if (!cls.contains("hunk")) seg.push(row);
  });
  flush();
}

if (typeof module !== "undefined") {
  module.exports = { splitLines, keepHeight };
} else {
  const hio = new IntersectionObserver((es) => es.forEach((e) => {
    if (e.isIntersecting && window.hljs) {
      highlightBlock(e.target, window.hljs);
      hio.unobserve(e.target);
    }
  }), { rootMargin: "400px" });
  document.querySelectorAll("pre code[class*=language-]").forEach((el) => hio.observe(el));
}
