"""Static inline-SVG figures for analyze-code HTML pages.

Write a small per-analysis generator that imports this module, places every node with explicit
coordinates, and writes one self-contained HTML file. The output is static SVG, so Lavish can
annotate each node and edge and exports render offline.

    import sys; sys.path.insert(0, "<this skill>/scripts")
    from figures import Fig, FIG_CSS, PAGE_JS, ARROW_ON, chip, report

    ov = Fig("overview", 1100, 500, "Who reaches ratelimit.py", resolve=my_resolver)
    ov.node("check", 350, 185, 200, "RateLimiter.check", "ratelimit.py:29", "target")
    ov.node("post", 20, 40, 250, "Handler.do_POST", "app/server.py:15", "entry", "POST /shorten")
    ov.edge("post", "check", [(270, 70), (350, 205)], "LIMITER.check(client)", (285, 62))
    html = f"<style>{FIG_CSS}</style> ... {ov.render()} ... {ARROW_ON}<script>{PAGE_JS}</script>"
    report()  # prints overflow warnings and the confirmed/inferred edge counts

Syntax-highlighted code: pass code="pub fn acquire(&self, key: K)" to node() for a signature or call
expression (it replaces the sans title line), and code="self.acquire(key).await?" to edge() for the call
site. Set the language once per figure: Fig(..., lang="rust"). Tokens become static <tspan> elements
(highlighted here with Pygments, not in the browser), so figures stay annotatable and render offline.

Refs display as the full path from the repository root: node() passes each ref through resolve(), so
`ratelimit.py:12 · :29` shows as `app/ratelimit.py:12,29`; a path too wide for its box
prints its directory on a small line above file:line. Give resolve() whatever maps your short refs.

Node kinds (CSS classes k-<kind>): normal, target (code inside the analyzed target), entry, external,
test (non-production caller), boundary (side-effect boundary, pill), route, decision, ok, fail.
Other marks: divider (a labelled phase line), raw (frames, headers, notes).
Edge kinds: solid (confirmed) and inferred (dashed; give it a "?" label and a tip saying why).

The page must define these CSS custom properties: --surface, --surface-2, --border-2, --text,
--text-2, --primary, --primary-tint, --accent, --ok, --ok-tint, --warn, --warn-tint, --test,
--test-tint, --sans, --mono.
"""
from __future__ import annotations  # `str | None` hints on Python 3.9

import html
import re

WARNINGS = []
EDGES = {"solid": 0, "inferred": 0}
TITLE_PX, REF_PX, LABEL_PX = 7.3, 6.75, 6.7  # per-char widths: Inter 13px, JetBrains Mono 11px
CODE_PX = 7.25                                # JetBrains Mono 12px, node code lines
DIR_PX = 6.15                                 # JetBrains Mono 10px, a full path's directory line

try:
    from pygments.lexers import get_lexer_by_name
    from pygments.token import Comment, Keyword, Name, Number, Operator, Punctuation, String
    from pygments.util import ClassNotFound
except ImportError:  # highlighting is a nicety: figures still render, as plain mono text
    get_lexer_by_name = None


def _token_class(tt) -> str | None:
    """Pygments token type -> one of the page's tk-* classes (colors in FIG_CSS)."""
    if get_lexer_by_name is None:
        return None
    for base, cls in ((Comment.Preproc, "tk-attr"), (Comment, "tk-com"), (Keyword.Type, "tk-type"), (Keyword, "tk-kw"),
                      (String, "tk-str"), (Number, "tk-num"), (Name.Decorator, "tk-attr"), (Name.Attribute, "tk-attr"),
                      (Name.Function, "tk-fn"), (Name.Class, "tk-type"), (Name.Namespace, "tk-type"),
                      (Name.Builtin.Pseudo, "tk-kw"), (Name.Builtin, "tk-bi"), (Name.Label, "tk-attr"),
                      (Operator, "tk-op"), (Punctuation, "tk-op")):
        if tt in base:
            return cls
    return None


def highlight(code: str, lang: str | None) -> str:
    """One line of code as SVG <tspan>s. Lexers often leave call targets and type paths as plain names,
    so a name followed by '(' or '!' reads as a function and a Capitalized name as a type."""
    code = code.replace("\n", " ")
    if get_lexer_by_name is None or not lang:
        if lang and get_lexer_by_name is None and "pygments" not in str(WARNINGS):
            WARNINGS.append("pygments not installed: code lines render without highlighting (pip install pygments)")
        return esc(code)
    try:
        lexer = get_lexer_by_name(lang)
    except ClassNotFound:
        WARNINGS.append(f"no lexer for language {lang!r}: code renders without highlighting")
        return esc(code)
    toks = [(tt, v) for tt, v in lexer.get_tokens(code) if v]
    if toks and toks[-1][1].endswith("\n"):  # lexers append a newline
        tt, v = toks[-1]
        toks[-1] = (tt, v[:-1])
    out = []
    for i, (tt, v) in enumerate(toks):
        cls = _token_class(tt)
        if cls is None and tt in Name:
            nxt = next((w for _, w in toks[i + 1:] if w.strip()), "")
            if nxt.startswith(("(", "!")):
                cls = "tk-fn"
            elif re.match(r"[A-Z]", v):
                cls = "tk-type"
        out.append(f'<tspan class="{cls}">{esc(v)}</tspan>' if cls else esc(v))
    return "".join(out)


def esc(s):
    return html.escape(str(s), quote=True)


REF_TOKEN = re.compile(r"([\w./-]+\.\w+):(\d+(?:-\d+)?)|(?<![\w.])[:,](\d+(?:-\d+)?)")


def full_ref(ref, resolve=lambda r: r):
    """The displayed form of a ref: every file as its full path from the repository root, with
    further lines in the same file appended (`app.py:966 · :1004` -> `scripts/x/app.py:966,1004`).
    Text that names no file:line is returned unchanged."""
    out, cur = [], None
    for m in REF_TOKEN.finditer(ref or ""):
        if m.group(1):
            path = resolve(f"{m.group(1)}:{m.group(2)}").rsplit(":", 1)[0]
            cur = [path, [m.group(2)]]
            out.append(cur)
        elif cur is not None:
            cur[1].append(m.group(3))
    if not out:
        return ref
    return " · ".join(f"{p}:{','.join(lines)}" for p, lines in out)


def chip(ref, label=None, resolve=lambda r: r):
    """A click-to-copy path:line chip for HTML prose. `resolve` maps a short ref to the copy target."""
    return f'<code class="ref" data-ref="{esc(resolve(ref))}" title="Click to copy">{esc(label or ref)}</code>'


class Fig:
    def __init__(self, fid, w, h, label, resolve=lambda r: r, lang=None):
        self.fid, self.w, self.h, self.label, self.resolve, self.lang = fid, w, h, label, resolve, lang
        self.parts = []
        self.nodes = {}

    def node(self, nid, x, y, w, title, ref=None, kind="normal", tag=None, h=56, tip=None, copy=None,
             code=None, lang=None):
        """A box with a title, an optional mono path:line line, and an optional tag above it.

        With code=, the title line shows that code syntax-highlighted (a signature or call expression)
        and the plain title stays as the tooltip. A tag sits 7px above the box, so leave room for it and
        keep edges from crossing it."""
        line_px = len(code) * CODE_PX if code else len(title) * TITLE_PX
        shown = full_ref(ref, self.resolve) if ref else None
        # a full path wider than the box prints its directory on a small line above file:line
        split = bool(shown) and len(shown) * REF_PX > w - 22 and "/" in shown.split(" · ")[0]
        ref_line = shown.split(" · ")[0].rsplit("/", 1)[1] + "".join(" · " + x for x in shown.split(" · ")[1:]) if split else shown
        dir_line = shown.split(" · ")[0].rsplit("/", 1)[0] + "/" if split else None
        if line_px > w - 22 or len(ref_line or "") * REF_PX > w - 22 or len(dir_line or "") * DIR_PX > w - 22:
            WARNINGS.append(f"{self.fid}:{nid} text wider than its box (w={w})")
        if x < 0 or y < 0 or x + w > self.w or y + h > self.h:
            WARNINGS.append(f"{self.fid}:{nid} outside the viewBox")
        for other, (ox, oy, ow, oh) in self.nodes.items():
            if x < ox + ow and ox < x + w and y < oy + oh and oy < y + h:
                WARNINGS.append(f"{self.fid}:{nid} overlaps {other}")
        self.nodes[nid] = (x, y, w, h)
        rx = h / 2 if kind == "boundary" else 12
        out = [f'<g class="node k-{kind}" data-id="{nid}" id="{self.fid}-{nid}" tabindex="0">',
               f'<title>{esc(tip or title)}</title>',
               f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}"/>']
        if tag:
            out.append(f'<text class="t-tag" x="{x + 2}" y="{y - 7}">{esc(tag)}</text>')
        first = (f'<text class="t-code" xml:space="preserve" x="{x + 14}" y="{{y}}">{highlight(code, lang or self.lang)}</text>'
                 if code else f'<text class="t-title" x="{x + 14}" y="{{y}}">{esc(title)}</text>')
        if ref and split:
            out.append(first.replace("{y}", str(y + h / 2 - 9)))
            out.append(f'<text class="t-dir" x="{x + 14}" y="{y + h / 2 + 5}">{esc(dir_line)}</text>')
            out.append(f'<text class="t-ref ref" data-ref="{esc(copy or self.resolve(ref))}" '
                       f'x="{x + 14}" y="{y + h / 2 + 19}">{esc(ref_line)}</text>')
        elif ref:
            out.append(first.replace("{y}", str(y + h / 2 - 3)))
            out.append(f'<text class="t-ref ref" data-ref="{esc(copy or self.resolve(ref))}" '
                       f'x="{x + 14}" y="{y + h / 2 + 14}">{esc(ref_line)}</text>')
        else:
            out.append(first.replace("{y}", str(y + h / 2 + 4)))
        out.append('</g>')
        self.parts.append("".join(out))

    def edge(self, a, b, pts, label=None, lpos=None, anchor="start", tip=None, kind="solid", code=None, lang=None):
        """A polyline from node a to node b through explicit points, drawn under the nodes.

        Prefer elbow points over long shallow diagonals. A label gets a solid background so lines
        under it stay legible. code= labels the edge with the highlighted call-site line instead
        (for example "let permit = self.acquire(key).await?"); keep it to one short line."""
        if code:
            label = code
        if a not in self.nodes or b not in self.nodes:
            WARNINGS.append(f"{self.fid}: edge {a}->{b} names a node that does not exist yet")
        EDGES[kind] += 1
        d = "M" + " L".join(f"{px},{py}" for px, py in pts)
        out = [f'<g class="edge e-{kind}" data-from="{a}" data-to="{b}">',
               f'<title>{esc(tip or label or (a + " → " + b))}</title>',
               f'<path d="{d}" marker-end="url(#arrow-{self.fid})"/>']
        if label:
            lx, ly = lpos
            lw = len(label) * LABEL_PX + 10
            bx = lx - 5 if anchor == "start" else (lx - lw / 2 if anchor == "middle" else lx - lw + 5)
            out.append(f'<rect class="lbg" x="{bx:.1f}" y="{ly - 12}" width="{lw:.1f}" height="16" rx="4"/>')
            body = highlight(code, lang or self.lang) if code else esc(label)
            cls = ' class="t-call" xml:space="preserve"' if code else ""
            out.append(f'<text{cls} x="{lx}" y="{ly}" text-anchor="{anchor}">{body}</text>')
        out.append('</g>')
        self.parts.insert(0, "".join(out))

    def divider(self, y, label, tip, x1=20, x2=None):
        """A dashed horizontal line marking a phase change, such as a write's dispatch point.

        The label sits right-aligned just below the line; it stays visible while a chain is focused."""
        x2 = x2 or self.w - 20
        self.parts.append(f'<g class="divider"><title>{esc(tip)}</title>'
                          f'<line x1="{x1}" y1="{y}" x2="{x2}" y2="{y}"/>'
                          f'<text x="{x2 - 2}" y="{y + 17}" text-anchor="end">{esc(label)}</text></g>')

    def raw(self, svg, under=False):
        """Frames, column headers, notes. Use under=True for frames so they sit beneath edges."""
        if under:
            self.parts.insert(0, svg)
        else:
            self.parts.append(svg)

    def render(self):
        defs = (f'<defs><marker id="arrow-{self.fid}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
                f'markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" class="arrowhead"/>'
                f'</marker></defs>')
        return (f'<svg class="fig" id="{self.fid}" viewBox="0 0 {self.w} {self.h}" role="img" '
                f'aria-label="{esc(self.label)}">{defs}{"".join(self.parts)}</svg>')


def report():
    print(f"edges: {EDGES['solid']} confirmed, {EDGES['inferred']} inferred")
    print("\n".join(WARNINGS) or "no layout warnings")


# Wrap each figure in <div class="figscroll"> so narrow screens scroll the figure, not the page.
FIG_CSS = """
.figscroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
.figscroll .fig{display:block;width:100%;min-width:820px;height:auto}
.fig .t-title{font:500 13px var(--sans);fill:var(--text)}
.fig .t-code,.fig .t-call{white-space:pre;font-variant-ligatures:none}
.fig .t-code{font:500 12px var(--mono);fill:var(--text)}
.fig .edge text.t-call{font:500 11px var(--mono);fill:#D4D4D8}
.fig .tk-kw{fill:#A5B4FC}.fig .tk-type{fill:#67E8F9}.fig .tk-fn{fill:#F0ABFC}.fig .tk-str{fill:#86EFAC}
.fig .tk-num{fill:#FDBA74}.fig .tk-com{fill:#71717A;font-style:italic}.fig .tk-attr{fill:#FCD34D}
.fig .tk-bi{fill:#93C5FD}.fig .tk-op{fill:#A1A1AA}
.fig .t-ref{font:500 11px var(--mono);fill:var(--text-2);cursor:copy}
.fig .t-ref:hover{fill:#C7D2FE}
.fig .t-dir{font:500 10px var(--mono);fill:var(--text-2);opacity:.7}
.fig .t-tag{font:600 9.5px var(--mono);letter-spacing:.08em;fill:var(--text-2)}
.fig .t-note{font:500 12px var(--sans);fill:var(--text-2)}
.fig .t-note.ref{font:500 11px var(--mono);cursor:copy}
.fig .node{cursor:pointer;transition:opacity .18s ease}
.fig .node rect{fill:var(--surface-2);stroke:var(--border-2);stroke-width:1.2}
.fig .node:focus{outline:none}
.fig .node:focus-visible rect{stroke:var(--text)}
.fig .k-target rect{fill:var(--primary-tint);stroke:var(--primary);stroke-width:1.6}
.fig .k-entry rect{stroke:#71717A}
.fig .k-external rect{fill:#18181B;stroke:#A1A1AA}
.fig .k-test rect{fill:var(--test-tint);stroke:var(--test)}
.fig .k-boundary rect{fill:var(--warn-tint);stroke:var(--warn);stroke-dasharray:5 4}
.fig .k-route rect{fill:#111;stroke:var(--border-2);rx:17px}
.fig .k-route .t-title{font:600 12px var(--mono)}
.fig .k-decision rect{fill:#1E1B4B;stroke:var(--accent)}
.fig .k-ok rect{fill:var(--ok-tint);stroke:var(--ok)}
.fig .k-fail rect{fill:var(--warn-tint);stroke:var(--warn)}
.fig .frame rect{fill:rgba(99,102,241,.04);stroke:rgba(99,102,241,.45);stroke-width:1.2;stroke-dasharray:2 4}
.fig .edge{transition:opacity .18s ease}
.fig .edge path{fill:none;stroke:#71717A;stroke-width:1.5}
.fig .e-inferred path{stroke-dasharray:6 5}
.fig .edge text{font:500 11px var(--mono);fill:var(--text-2)}
.fig .lbg{fill:var(--surface)}
.fig .arrowhead{fill:#71717A}
.fig .divider line{stroke:var(--primary);stroke-width:2;stroke-dasharray:8 6}
.fig .divider text{font:600 10.5px var(--mono);letter-spacing:.06em;fill:#A5B4FC}
.fig.focus .node,.fig.focus .edge,.fig.focus .frame,.fig.focus .t-note{opacity:.16}
.fig.focus .node.on,.fig.focus .edge.on{opacity:1}
.fig.focus .edge.on path{stroke:var(--primary);marker-end:url(#arrow-on)}
code.ref{cursor:copy}
.toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%) translateY(20px);opacity:0;pointer-events:none;
  background:var(--surface);border:1px solid var(--primary);color:var(--text);font:500 13px var(--mono);
  padding:10px 16px;border-radius:9999px;transition:all .2s ease;max-width:calc(100vw - 32px);overflow-wrap:anywhere}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
"""

# Place once in <body>: the highlighted arrowhead and the copy toast.
ARROW_ON = ('<svg width="0" height="0" style="position:absolute" aria-hidden="true"><defs>'
            '<marker id="arrow-on" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
            'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#6366F1"/></marker></defs></svg>'
            '<div class="toast" id="toast" role="status" aria-live="polite"></div>')

# Click a [data-ref] to copy it; click a node to highlight every chain through it; click empty space to clear.
PAGE_JS = """
(function(){
  const toast=document.getElementById('toast');let timer;
  function show(msg){toast.textContent=msg;toast.classList.add('show');clearTimeout(timer);timer=setTimeout(()=>toast.classList.remove('show'),1400);}
  async function copy(ref){
    try{await navigator.clipboard.writeText(ref);show('Copied '+ref);}
    catch(e){const t=document.createElement('textarea');t.value=ref;document.body.appendChild(t);t.select();
      try{document.execCommand('copy');show('Copied '+ref);}catch(_e){show(ref);}t.remove();}
  }
  document.addEventListener('click',e=>{const r=e.target.closest('[data-ref]');
    if(r){e.stopPropagation();copy(r.getAttribute('data-ref'));}},true);
  document.querySelectorAll('svg.fig').forEach(svg=>{
    const edges=[...svg.querySelectorAll('.edge')];
    function walk(start,dir){
      const seen=new Set([start]);const stack=[start];const hit=new Set();
      while(stack.length){const n=stack.pop();
        edges.forEach(e=>{const a=e.dataset.from,b=e.dataset.to;
          const nxt=dir==='down'?(a===n?b:null):(b===n?a:null);
          if(nxt){hit.add(e);if(!seen.has(nxt)){seen.add(nxt);stack.push(nxt);}}});}
      return [seen,hit];
    }
    function clear(){svg.classList.remove('focus');svg.querySelectorAll('.on').forEach(x=>x.classList.remove('on'));}
    function focus(node){
      const id=node.dataset.id;
      if(svg.classList.contains('focus')&&svg.dataset.sel===id){clear();svg.dataset.sel='';return;}
      clear();svg.dataset.sel=id;
      const [up,eu]=walk(id,'up');const [dn,ed]=walk(id,'down');
      svg.classList.add('focus');
      svg.querySelectorAll('.node').forEach(n=>{if(up.has(n.dataset.id)||dn.has(n.dataset.id))n.classList.add('on');});
      [...eu,...ed].forEach(e=>e.classList.add('on'));
    }
    svg.querySelectorAll('.node').forEach(n=>{
      n.addEventListener('click',e=>{e.stopPropagation();focus(n);});
      n.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();focus(n);}});
    });
    svg.addEventListener('click',clear);
  });
})();
"""
