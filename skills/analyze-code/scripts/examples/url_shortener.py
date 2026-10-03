"""Worked example: the analyze-code page for app/ratelimit.py in a small URL shortener demo.

The demo repo is a tiny HTTP service: app/server.py (Handler.do_POST / do_GET on a ThreadingHTTPServer),
app/ratelimit.py (TokenBucket, RateLimiter), app/store.py (LinkStore with expiry), app/ids.py and tests/.
The analysis target is RateLimiter.check and the rate-limiting path behind POST /shorten.

Shows the four figure patterns: overview with a frame, route map, decision tree, and lanes with a divider.
The facts are a snapshot of that analysis; reuse the layout patterns, not the content.
Usage: python3 url_shortener.py <out.html>
"""
import html
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))  # scripts/, next to figures.py
from figures import Fig as _Fig, EDGES, FIG_CSS, PAGE_JS, ARROW_ON, full_ref as display_ref, report

OUT = sys.argv[1]
RL = "app/ratelimit.py"


def esc(s):
    return html.escape(str(s), quote=True)


def resolve(ref):
    """Map a short ref (`server.py:18`, `:29`) to its path from the repository root."""
    table = {
        "ratelimit.py": RL, "server.py": "app/server.py", "store.py": "app/store.py", "ids.py": "app/ids.py",
        "test_ratelimit.py": "tests/test_ratelimit.py", "test_store.py": "tests/test_store.py",
    }
    head = ref.split(" ")[0]
    if head.startswith(":"):
        return RL + head
    name, _, line = head.partition(":")
    return table.get(name, name) + (":" + line if line else "")


def Fig(fid, w, h, label):
    return _Fig(fid, w, h, label, resolve=resolve, lang="python")  # code= lines highlight as Python


# ---------------------------------------------------------------- overview
ov = Fig("overview", 1100, 510, "Overview of who reaches app/ratelimit.py and what it reaches")
ov.raw('<g class="frame" id="overview-frame"><title>app/ratelimit.py</title>'
       '<rect x="400" y="130" width="680" height="245" rx="22"/>'
       '<text class="t-tag" x="1068" y="367" text-anchor="end">APP/RATELIMIT.PY</text></g>', under=True)
ov.node("main", 20, 40, 250, "main", "server.py:49-50", "entry", "ENTRY · PROCESS",
        code="ThreadingHTTPServer(…, Handler)",
        tip="main() serves Handler on 127.0.0.1:8080; nothing in the repo calls main()")
ov.node("limiter", 430, 40, 290, "LIMITER", "server.py:11", "normal", "MODULE GLOBAL · ONE PER PROCESS",
        code="LIMITER = RateLimiter(capacity=5, …)",
        tip="Built once at import with capacity=5, refill_per_sec=1.0; every request thread uses this instance")
ov.node("dopost", 20, 160, 250, "Handler.do_POST", "server.py:15 · :18", "normal", "HTTP HANDLER",
        code="Handler.do_POST(self)", tip="POST /shorten; the only production caller of check")
ov.node("t1", 20, 280, 250, "test_burst_then_limited", "test_ratelimit.py:4", "test", "TESTS",
        code="test_burst_then_limited", tip="Five calls return 0, the sixth returns > 0")
ov.node("t2", 20, 360, 250, "test_clients_are_independent", "test_ratelimit.py:10", "test",
        code="test_clients_are_independent", tip="capacity=1: clients 'a' and 'b' each get their own token")
ov.node("check", 430, 160, 250, "RateLimiter.check", "ratelimit.py:29-33", "target", "RATELIMITER",
        code="def check(self, client)", tip="Find or create the client's bucket, then spend from it")
ov.node("buckets", 870, 160, 200, "shared dict · no lock", "ratelimit.py:27 · :32", "boundary",
        "BOUNDARY · SHARED STATE",
        tip="self.buckets: one TokenBucket per client key, read and written by every request thread, never evicted")
ov.node("take", 430, 290, 250, "TokenBucket.take", "ratelimit.py:12-20", "target", "TOKENBUCKET",
        code="def take(self)", tip="Refill from elapsed time, then spend one token or return the wait")
ov.node("ctor", 760, 290, 240, "TokenBucket.__init__", "ratelimit.py:6-10", "target", "ON FIRST REQUEST",
        code="TokenBucket.__init__(…)", tip="A new bucket starts full: tokens = capacity (ratelimit.py:9)")
ov.node("clock", 520, 430, 340, "time.time() · wall clock", "ratelimit.py:10 · :14", "boundary", "BOUNDARY · CLOCK",
        tip="time.time() is wall-clock time, not monotonic")
ov.edge("main", "dopost", [(190, 96), (190, 160)], "thread per request", (200, 124),
        tip="ThreadingHTTPServer runs each request's handler on its own thread (server.py:50)")
ov.edge("limiter", "check", [(575, 96), (575, 160)], "same instance in every thread", (585, 116),
        tip="All threads call check on the one LIMITER, so they share its buckets dict")
ov.edge("dopost", "check", [(270, 176), (430, 176)], code="LIMITER.check(…)", lpos=(335, 180), anchor="middle",
        tip='LIMITER.check(self.headers.get("X-Forwarded-For", self.client_address[0])) (server.py:18)')
ov.edge("t1", "check", [(270, 308), (380, 308), (380, 194), (430, 194)], code='rl.check("ip")', lpos=(325, 312),
        anchor="middle", tip="tests/test_ratelimit.py:6-7")
ov.edge("t2", "check", [(270, 388), (392, 388), (392, 208), (430, 208)], code='rl.check("a")', lpos=(330, 392),
        anchor="middle", tip="tests/test_ratelimit.py:12-13")
ov.edge("check", "buckets", [(680, 188), (870, 188)], code="self.buckets.get(client)", lpos=(775, 192),
        anchor="middle", tip="Read at ratelimit.py:30, written at ratelimit.py:32")
ov.edge("check", "take", [(540, 216), (540, 290)], code="bucket.take()", lpos=(550, 258), tip="ratelimit.py:33")
ov.edge("check", "ctor", [(650, 216), (650, 250), (880, 250), (880, 290)], code="TokenBucket(self.capacity, …)",
        lpos=(765, 243), anchor="middle", tip="Only when buckets has no entry for the client (ratelimit.py:31-32)")
ov.edge("take", "clock", [(650, 346), (650, 430)], code="now = time.time()", lpos=(642, 398), anchor="end",
        tip="ratelimit.py:14")
ov.edge("ctor", "clock", [(800, 346), (800, 430)], code="self.updated = time.time()", lpos=(808, 398),
        tip="ratelimit.py:10")

# ---------------------------------------------------------------- routes
rt = Fig("routes", 1100, 510, "Route map from each HTTP route to its response")
for cx, label in ((20, "ROUTE"), (180, "HANDLER"), (360, "CALLS"), (920, "RESPONSE")):
    rt.raw(f'<text class="t-tag" x="{cx + 2}" y="32">{label}</text>')
rt.raw('<line x1="20" y1="245" x2="1080" y2="245" stroke="var(--border-2)" stroke-width="1" stroke-dasharray="2 5"/>',
       under=True)
# POST /shorten
rt.node("rpost", 20, 70, 130, "POST /shorten", None, "route", h=36, tip="Any other POST path gets 404 (server.py:16-17)")
rt.node("dopost", 180, 60, 150, "Handler.do_POST", "server.py:15", code="do_POST(self)")
rt.node("check", 360, 60, 160, "RateLimiter.check", "ratelimit.py:29", "target", code="LIMITER.check(…)",
        tip="Keyed by X-Forwarded-For, else the peer IP (server.py:18)")
rt.node("r429", 920, 60, 160, "429 · Retry-After", "server.py:19-22", "fail",
        tip="Retry-After = str(math.ceil(wait)); no body (server.py:20-22)")
rt.node("body", 360, 160, 160, "parse body", "server.py:24", code="json.loads(…)",
        tip="json.loads(self.rfile.read(int(self.headers['Content-Length'])))")
rt.node("newcode", 550, 160, 150, "new_code", "ids.py:8", code="new_code()", tip="7 characters from secrets.choice")
rt.node("put", 730, 160, 160, "LinkStore.put", "store.py:9", code="STORE.put(…)",
        tip='STORE.put(code, body["url"], body.get("ttlSeconds")) (server.py:26)')
rt.node("r201", 920, 160, 160, "201 · {code}", "server.py:27", "ok", tip='self._json(201, {"code": code})')
rt.edge("rpost", "dopost", [(150, 88), (180, 88)])
rt.edge("dopost", "check", [(330, 88), (360, 88)])
rt.edge("check", "r429", [(520, 88), (920, 88)], code="if wait:", lpos=(725, 92), anchor="middle",
        tip="Any nonzero wait means limited (server.py:19)")
rt.edge("check", "body", [(440, 116), (440, 160)], "returned 0", (450, 142), tip="wait == 0: allowed")
rt.edge("body", "newcode", [(520, 188), (550, 188)])
rt.edge("newcode", "put", [(700, 188), (730, 188)])
rt.edge("put", "r201", [(890, 188), (920, 188)])
# GET /<code>
rt.node("rget", 20, 290, 130, "GET /<code>", None, "route", h=36, tip="Every GET path is treated as a code; no rate limit")
rt.node("doget", 180, 280, 150, "Handler.do_GET", "server.py:29", code="do_GET(self)")
rt.node("get", 360, 280, 160, "LinkStore.get", "store.py:13", code="STORE.get(…)",
        tip='STORE.get(self.path.lstrip("/")) (server.py:30)')
rt.node("r404", 920, 280, 160, "404 Not Found", "server.py:31-32", "fail", tip="Unknown code")
rt.node("r410", 920, 360, 160, "410 · expired", "server.py:34-35", "fail",
        tip="expires is set and time.time() >= expires (store.py:19)")
rt.node("r302", 920, 440, 160, "302 · Location", "server.py:36-38", "ok", tip="Location: the stored URL")
rt.edge("rget", "doget", [(150, 308), (180, 308)])
rt.edge("doget", "get", [(330, 308), (360, 308)])
rt.edge("get", "r404", [(520, 308), (920, 308)], code="if found is None:", lpos=(725, 312), anchor="middle",
        tip="server.py:31")
rt.edge("get", "r410", [(480, 336), (480, 388), (920, 388)], code="if expired:", lpos=(725, 392), anchor="middle",
        tip="server.py:34")
rt.edge("get", "r302", [(420, 336), (420, 468), (920, 468)], code="self.send_response(302)", lpos=(725, 472),
        anchor="middle", tip="server.py:36")

# ---------------------------------------------------------------- take() decision tree
tk = Fig("take", 1100, 550, "How TokenBucket.take decides, and what do_POST does with the result")
tk.node("call", 400, 20, 300, "RateLimiter.check", "ratelimit.py:33", code="return bucket.take()",
        tip="check returns whatever take returns")
tk.node("refill", 380, 120, 340, "refill", "ratelimit.py:14-16", "target", code="self.tokens = min(self.capacity, …)",
        tip="tokens = min(capacity, tokens + (now - updated) * refill_per_sec); updated = now")
tk.raw('<text class="t-note" x="740" y="144">+ (now − updated) × refill_per_sec</text>'
       '<text class="t-note" x="740" y="162">capped at capacity · updated = now</text>')
tk.node("dec", 400, 230, 300, "tokens >= 1?", "ratelimit.py:17", "decision", code="if self.tokens >= 1:",
        tip="At least one whole token after the refill")
tk.node("spend", 150, 350, 300, "spend one token", "ratelimit.py:18", "ok", "YES · ALLOWED",
        code="self.tokens -= 1", tip="Spend one token, then return 0 (ratelimit.py:19)")
tk.node("wait", 600, 350, 360, "seconds until the next token", "ratelimit.py:20", "fail", "NO · LIMITED",
        code="return (1 - self.tokens) / self.refill_per_sec",
        tip="Always > 0 here because tokens < 1; nothing is spent")
tk.node("ok201", 150, 470, 300, "201 with the new code", "server.py:24-27", "ok", "DO_POST · 201",
        code='self._json(201, {"code": code})', tip="Parse the body, new_code(), STORE.put, then 201")
tk.node("r429", 600, 470, 360, "429 with Retry-After", "server.py:19-22", "fail", "DO_POST · 429",
        code='self.send_header("Retry-After", …)', tip="Retry-After = str(math.ceil(wait)) (server.py:21)")
tk.edge("call", "refill", [(550, 76), (550, 120)])
tk.edge("refill", "dec", [(550, 176), (550, 230)])
tk.edge("dec", "spend", [(480, 286), (480, 318), (300, 318), (300, 350)], "yes", (390, 322), anchor="middle")
tk.edge("dec", "wait", [(620, 286), (620, 318), (780, 318), (780, 350)], "no", (700, 322), anchor="middle")
tk.edge("spend", "ok201", [(300, 406), (300, 470)], code="return 0", lpos=(310, 442),
        tip="do_POST sees wait == 0 and skips the 429 branch (server.py:19)")
tk.edge("wait", "r429", [(780, 406), (780, 470)], code="if wait:", lpos=(790, 442),
        tip="do_POST sees a nonzero wait (server.py:19)")

# ---------------------------------------------------------------- race between two request threads
RACE = [
    # (row, code, ref, kind, tag for lane A, tag for lane B, tip)
    (0, "LIMITER.check(…)", "server.py:18", "normal", None, None,
     "Same client key, so both threads get the same bucket from buckets.get (ratelimit.py:30)"),
    (1, "if self.tokens >= 1:", "ratelimit.py:17", "target", "READS TOKENS ≈ 1.0", "READS TOKENS ≈ 1.0",
     "After the refill at ratelimit.py:15 the bucket holds just over 1 token, and neither thread has spent yet"),
    (2, "self.tokens -= 1", "ratelimit.py:18", "target", "TOKENS 1.0 → 0.0", "TOKENS 0.0 → -1.0",
     "Both spend. If both read tokens before either writes, the second write overwrites the first and only "
     "one token is deducted"),
    (3, 'self._json(201, {"code": code})', "server.py:26-27", "normal", None, None,
     "STORE.put stores the link (server.py:26), then the 201 goes out (server.py:27)"),
]
LX = {"A": 40, "B": 400}
SW, SH = 300, 44


def ry(row, lane):
    return 70 + row * 84 + (38 if lane == "B" else 0) + (70 if row >= 3 else 0)


rc = Fig("race", 1000, ry(3, "B") + SH + 66, "Two request threads from one client racing through take() without a lock")
rc.raw('<text class="t-tag" x="42" y="40">THREAD A · REQUEST 1</text>'
       '<text class="t-tag" x="402" y="40">THREAD B · REQUEST 2, SAME CLIENT</text>'
       '<text class="t-tag" x="762" y="40">OUTCOME</text>')
rc.divider(ry(2, "B") + SH + 40, "RESPONSE SENT · app/server.py:27 · cannot be undone",
           "Point of no return: the link is stored and the 201 is written; an over-admitted request cannot be "
           "taken back (server.py:26-27)")
for lane in ("A", "B"):
    for row, code, ref, kind, tag_a, tag_b, tip in RACE:
        y = ry(row, lane)
        rc.node(f"{lane}{row}", LX[lane], y, SW, code, ref, kind, tag_a if lane == "A" else tag_b, h=SH,
                tip=f"Thread {lane}: {tip}", code=code)
        if row:
            x = LX[lane] + SW / 2
            top = ry(row - 1, lane) + SH
            if row == 3:
                rc.edge(f"{lane}{row - 1}", f"{lane}{row}", [(x, top), (x, y)], code="return 0",
                        lpos=(x + 10, ry(2, "B") + SH + 26), tip="check returns 0, so do_POST goes on to store the link")
            else:
                rc.edge(f"{lane}{row - 1}", f"{lane}{row}", [(x, top), (x, y)])
rc.node("both", 760, ry(0, "A"), 220, "2 admitted on 1 token", "ratelimit.py:17-18", "fail",
        h=ry(2, "B") + SH - ry(0, "A"),
        tip="Both threads pass tokens >= 1 before either spends; tokens ends at 0.0 or -1.0, and both calls return 0")
rc.node("over", 760, ry(3, "A"), 220, "201 × 2 · over the limit", "server.py:26-27", "fail",
        h=ry(3, "B") + SH - ry(3, "A"), tip="Two links stored and two codes returned for one token")
rc.raw(f'<text class="t-note" x="760" y="{ry(3, "B") + SH + 30}">With one lock around take():</text>'
       f'<text class="t-note" x="760" y="{ry(3, "B") + SH + 48}">B reads 0.0 → 429, Retry-After: 1</text>')

# ---------------------------------------------------------------- page


def chip(ref, label=None):
    return (f'<code class="ref" data-ref="{esc(resolve(ref))}" title="Click to copy">'
            f'{esc(label or display_ref(ref, resolve))}</code>')


C = chip
TREE = """main()                                   # app/server.py:49  entry: process, no caller in repo
└─ ThreadingHTTPServer(.., Handler)      # app/server.py:50  one thread per request
   └─ Handler.do_POST(self)              # app/server.py:15  POST /shorten only
      └─ LIMITER.check(client)           # app/server.py:18  X-Forwarded-For or peer IP
         └─ ▶ RateLimiter.check(client)  # app/ratelimit.py:29  <- target
            ├─ self.buckets.get(client)  # app/ratelimit.py:30  boundary: shared dict, no lock
            ├─ TokenBucket(..)           # app/ratelimit.py:32  first request per client
            │  └─ time.time()            # app/ratelimit.py:10  boundary: clock
            └─ bucket.take()             # app/ratelimit.py:33
               └─ time.time()            # app/ratelimit.py:14  boundary: clock
# tests: tests/test_ratelimit.py:4,10"""

page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rate limiter lineage</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@500;600&display=swap" rel="stylesheet">
<style>
:root {{
  color-scheme: dark;
  --bg:#050505; --surface:#161616; --surface-2:#1c1c1f; --border:#262626; --border-2:#3f3f46;
  --text:#FFFFFF; --text-2:#A1A1AA; --primary:#6366F1; --accent:#4F46E5;
  --primary-tint:rgba(99,102,241,.14); --ok:#34D399; --ok-tint:rgba(52,211,153,.10);
  --warn:#F59E0B; --warn-tint:rgba(245,158,11,.10); --test:#22D3EE; --test-tint:rgba(34,211,238,.08);
  --r-card:32px; --r-control:48px; --r-pill:9999px;
  --mono:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,monospace;
  --sans:Inter,ui-sans-serif,system-ui,-apple-system,sans-serif;
}}
*{{box-sizing:border-box}}
html,body{{margin:0;background:var(--bg);color:var(--text)}}
body{{font:400 16px/1.6 var(--sans);-webkit-font-smoothing:antialiased}}
.wrap{{max-width:1180px;margin:0 auto;padding:0 24px}}
header.hero{{padding:80px 0 48px;border-bottom:1px solid var(--border)}}
.eyebrow,.label{{font:600 12px/1.2 var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--text-2)}}
.question{{margin:20px 0 12px;color:var(--text-2);font-size:18px}}
h1{{font:500 clamp(34px,5.4vw,64px)/1.04 var(--sans);letter-spacing:0;margin:0;max-width:26ch}}
h1 em{{font-style:normal;color:var(--primary)}}
.chips{{display:flex;flex-wrap:wrap;gap:8px;margin-top:28px}}
.chip{{font:600 12px/1.2 var(--mono);padding:9px 14px;border-radius:var(--r-pill);background:var(--surface);
  border:1px solid var(--border);color:var(--text-2);min-width:0;overflow-wrap:anywhere}}
.chip b{{color:var(--text);font-weight:600}}
section{{padding:72px 0 0}}
section:last-of-type{{padding-bottom:96px}}
h2{{font:500 32px/1.15 var(--sans);margin:12px 0 8px}}
h3{{font:500 20px/1.3 var(--sans);margin:0 0 8px}}
.lead{{color:var(--text-2);max-width:72ch;margin:0 0 24px}}
.card{{background:var(--surface);border:1px solid var(--border);border-radius:var(--r-card);padding:24px;
  box-shadow:0 1px 0 rgba(255,255,255,.03) inset,0 24px 48px -24px rgba(0,0,0,.6);min-width:0}}
.grid{{display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(min(100%,320px),1fr))}}
.grid > *{{min-width:0}}
.card p{{margin:0 0 10px;color:#D4D4D8}}
.card p:last-child{{margin-bottom:0}}
.figure{{padding:20px 20px 12px}}
.figcap{{display:flex;flex-wrap:wrap;gap:8px 20px;align-items:center;justify-content:space-between;
  color:var(--text-2);font-size:13px;margin:12px 4px 0}}
.legend{{display:flex;flex-wrap:wrap;gap:6px 16px}}
.legend span{{display:inline-flex;align-items:center;gap:6px;font:500 12px var(--mono)}}
.sw{{width:14px;height:10px;border-radius:3px;border:1.5px solid;display:inline-block}}
code,.mono{{font-family:var(--mono);font-variant-ligatures:none}}
code.ref{{font:500 12.5px var(--mono);color:#C7D2FE;background:var(--primary-tint);border:1px solid rgba(99,102,241,.3);
  padding:1px 7px;border-radius:var(--r-pill);cursor:copy;white-space:nowrap}}
code.ref:hover{{border-color:var(--primary)}}
p code:not(.ref),td code:not(.ref),li code:not(.ref){{font-size:13px;color:#E4E4E7;background:#222;padding:1px 6px;border-radius:6px}}
{{FIG_CSS_BLOCK}}
/* tables */
.tablewrap{{overflow-x:auto;border:1px solid var(--border);border-radius:24px;background:var(--surface)}}
table{{border-collapse:collapse;width:100%;min-width:720px;font-size:14px}}
th,td{{text-align:left;padding:14px 18px;border-bottom:1px solid var(--border);vertical-align:top}}
th{{font:600 11px var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--text-2);background:#121212}}
tr:last-child td{{border-bottom:0}}
td{{color:#D4D4D8}}
.badge{{display:inline-block;font:600 11px/1 var(--mono);padding:6px 10px;border-radius:var(--r-pill);white-space:nowrap}}
.b-ok{{color:var(--ok);background:var(--ok-tint);border:1px solid rgba(52,211,153,.35)}}
.b-warn{{color:var(--warn);background:var(--warn-tint);border:1px solid rgba(245,158,11,.35)}}
.b-test{{color:var(--test);background:var(--test-tint);border:1px solid rgba(34,211,238,.35)}}
.callout{{border-left:3px solid var(--warn)}}
.unconfirmed{{border:1px solid rgba(245,158,11,.45);background:linear-gradient(180deg,rgba(245,158,11,.06),transparent 60%),var(--surface)}}
ul.clean{{margin:0;padding-left:18px;color:#D4D4D8}}
ul.clean li{{margin:0 0 10px}}
ul.clean li:last-child{{margin-bottom:0}}
details.ascii{{margin-top:12px}}
details.ascii summary{{cursor:pointer;color:var(--text-2);font:600 12px var(--mono);letter-spacing:.06em;text-transform:uppercase;padding:6px 4px}}
pre{{margin:10px 0 0;padding:18px;background:#0B0B0C;border:1px solid var(--border);border-radius:20px;overflow-x:auto;
  font:500 12px/1.55 var(--mono);color:#D4D4D8}}
@media (max-width:640px){{
  .wrap{{padding:0 16px}}
  header.hero{{padding:48px 0 32px}}
  section{{padding-top:48px}}
  h2{{font-size:26px}}
  .card{{border-radius:24px;padding:18px}}
}}
@media (prefers-reduced-motion:reduce){{*{{transition:none!important}}}}
</style>
</head>
<body>
<header class="hero"><div class="wrap">
  <div class="eyebrow">analyze-code · symbol in its file · default depth</div>
  <p class="question" id="q">What is <code>RateLimiter.check</code> for, and how does a request reach it?</p>
  <h1 id="role">The per-client token bucket that <em>decides whether POST /shorten may create a link</em>.</h1>
  <div class="chips">
    <span class="chip">defined at <b>{C("ratelimit.py:29")}</b></span>
    <span class="chip">kind <b>method on RateLimiter</b></span>
    <span class="chip">language <b>Python</b></span>
    <span class="chip">trace <b>entry to leaves</b></span>
    <span class="chip">edges <b>{{EDGES}} confirmed · {{INFERRED}} inferred</b></span>
  </div>
</div></header>

<main class="wrap">

<section id="overview-section">
  <div class="label">01 · Overview</div>
  <h2>Where the rate limiter sits</h2>
  <p class="lead">One production caller and two tests reach <code>check</code>. Every request thread calls it on the same <code>LIMITER</code>, so the buckets dict is shared state with no lock. Click a node to trace its chain; click any <span class="mono">path:line</span> to copy it.</p>
  <div class="card figure">
    <div class="figscroll">{ov.render()}</div>
    <div class="figcap">
      <div class="legend">
        <span><i class="sw" style="border-color:var(--primary);background:var(--primary-tint)"></i>in app/ratelimit.py</span>
        <span><i class="sw" style="border-color:#71717A"></i>entry point</span>
        <span><i class="sw" style="border-color:var(--test);background:var(--test-tint)"></i>test</span>
        <span><i class="sw" style="border-color:var(--warn);border-style:dashed;background:var(--warn-tint)"></i>side-effect boundary</span>
      </div>
      <span>All edges confirmed by reading both ends.</span>
    </div>
  </div>
  <details class="ascii"><summary>ASCII tree (copyable)</summary><pre>{esc(TREE)}</pre></details>
</section>

<section id="routes-section">
  <div class="label">02 · Routes</div>
  <h2>Only POST /shorten is limited</h2>
  <p class="lead">The handler class has two methods, and only <code>do_POST</code> calls the limiter. A limited request returns before the body is read. <code>GET /&lt;code&gt;</code> never touches the limiter.</p>
  <div class="card figure">
    <div class="figscroll">{rt.render()}</div>
    <div class="figcap"><span><code>BaseHTTPRequestHandler</code> dispatches each request to <code>do_&lt;METHOD&gt;</code> on {C("server.py:14", "Handler")}. <code>do_POST</code> answers 404 for any path other than <code>/shorten</code> before it reaches the limiter ({C("server.py:16-17")}).</span></div>
  </div>
</section>

<section id="outbound">
  <div class="label">03 · Outbound</div>
  <h2>What it depends on</h2>
  <div class="tablewrap"><table>
    <thead><tr><th>Dependency</th><th>Used for</th><th>Where</th><th>Boundary</th></tr></thead>
    <tbody>
      <tr id="out-buckets"><td><code>self.buckets</code></td><td>One <code>TokenBucket</code> per client key. Entries are created on first use and never removed.</td><td>{C("ratelimit.py:27")} {C("ratelimit.py:30-32")}</td><td><span class="badge b-warn">shared dict · no lock</span></td></tr>
      <tr id="out-bucket"><td><code>TokenBucket</code></td><td>The per-client counter: <code>tokens</code>, <code>updated</code>, <code>take()</code>.</td><td>{C("ratelimit.py:5-20")}</td><td>-</td></tr>
      <tr id="out-clock"><td><code>time.time()</code></td><td>Creation stamp and the elapsed time behind each refill.</td><td>{C("ratelimit.py:10")} {C("ratelimit.py:14")}</td><td><span class="badge b-warn">wall clock</span></td></tr>
    </tbody></table></div>
</section>

<section id="role-section">
  <div class="label">04 · Role</div>
  <h2>Why it exists</h2>
  <div class="grid">
    <div class="card" id="role-what"><h3>Role</h3>
      <p>It is the only thing that caps how fast one client can create links. Delete it and <code>do_POST</code> would store a link for every request ({C("server.py:24-27")}), and nothing would ever answer 429.</p></div>
    <div class="card" id="role-why"><h3>Why here</h3>
      <p><code>do_POST</code> only picks the client key ({C("server.py:18")}) and turns a wait into <code>Retry-After</code> ({C("server.py:21")}). The bucket math lives in <code>app/ratelimit.py</code>, which imports nothing but <code>time</code>, so the tests can drive it without a server ({C("test_ratelimit.py:4")}).</p></div>
  </div>
</section>

<section id="dataflow">
  <div class="label">05 · Data flow and side effects</div>
  <h2>Two mechanisms carry the limiter</h2>

  <h3 style="margin-top:28px" id="take-title">1. <code>take</code> refills, then spends or returns a wait</h3>
  <p class="lead">The return value is the whole contract: <code>0</code> means allowed, anything else is seconds to wait. Hover a node for its exact condition.</p>
  <div class="card figure"><div class="figscroll">{tk.render()}</div>
    <div class="figcap"><span>Tokens are floats, so a client 0.4 s after its last token sees <code>wait ≈ 0.6</code>, and <code>math.ceil</code> rounds the header up to <code>Retry-After: 1</code> ({C("server.py:21")}).</span></div></div>

  <h3 style="margin-top:40px" id="race-title">2. Two threads from one client can both spend the last token</h3>
  <p class="lead"><code>ThreadingHTTPServer</code> runs each request on its own thread ({C("server.py:50")}), and nothing locks the bucket between the check and the spend. This is one possible interleaving; it is read from the code, not reproduced.</p>
  <div class="card figure"><div class="figscroll">{rc.render()}</div>
    <div class="figcap"><span>Before the dashed line the overspend is only a number in memory. After it, two links exist and both clients hold a code.</span></div></div>

  <div class="grid" style="margin-top:16px">
    <div class="card" id="state"><h3>State it owns</h3><p><code>buckets</code> on the limiter ({C("ratelimit.py:27")}), and <code>tokens</code> and <code>updated</code> on each bucket ({C("ratelimit.py:9-10")}). Each call writes all of them, from whichever request thread is running.</p></div>
    <div class="card" id="pre"><h3>Preconditions</h3><p><code>refill_per_sec</code> must be nonzero, because the wait divides by it ({C("ratelimit.py:20")}). <code>client</code> must be hashable; the server passes a header string or an IP string ({C("server.py:18")}).</p></div>
    <div class="card" id="post"><h3>Postconditions</h3><p>On <code>0</code>, the bucket holds one token less. Otherwise nothing is spent and the result is positive ({C("ratelimit.py:17-20")}). After any call, <code>tokens</code> is at most <code>capacity</code> ({C("ratelimit.py:15")}).</p></div>
  </div>
</section>

<section id="impact">
  <div class="label">06 · Change impact</div>
  <h2>What breaks if it changes</h2>
  <div class="tablewrap"><table>
    <thead><tr><th>Surface</th><th>Who breaks</th><th>Tests</th></tr></thead>
    <tbody>
      <tr id="imp-return"><td>Return value of <code>check</code> {C("ratelimit.py:33")}</td><td><code>do_POST</code> treats any truthy value as limited ({C("server.py:19")}) and passes it to <code>math.ceil</code> ({C("server.py:21")}). Returning <code>True</code> would always send <code>Retry-After: 1</code>; returning <code>None</code> would admit every request.</td><td><code>test_burst_then_limited</code> {C("test_ratelimit.py:4")}</td></tr>
      <tr id="imp-key"><td>Client key {C("server.py:18")}</td><td>Who shares a bucket. Keys come from <code>X-Forwarded-For</code> when present, which the client controls.</td><td><code>test_clients_are_independent</code> {C("test_ratelimit.py:10")} covers separate keys, not how the server picks them.</td></tr>
      <tr id="imp-limits"><td>Limits {C("server.py:11")}</td><td>Burst of 5, then 1 per second. <code>config.toml</code> repeats these values, but nothing reads it.</td><td><span class="badge b-warn">untested</span></td></tr>
      <tr id="imp-threads"><td>Concurrency</td><td>Any change to <code>check</code> or <code>take</code> runs on many request threads at once.</td><td><span class="badge b-warn">untested</span></td></tr>
    </tbody></table></div>
  <div class="grid" style="margin-top:16px">
    <div class="card callout" id="couple-race"><h3>No lock around the bucket</h3><p>Two threads can both pass <code>tokens &gt;= 1</code> before either spends ({C("ratelimit.py:17-18")}). Two first requests from a new client can also each create a bucket ({C("ratelimit.py:30-32")}); the second assignment wins, so that client gets one extra request in its first burst.</p></div>
    <div class="card callout" id="couple-clock"><h3>Wall clock, not monotonic</h3><p><code>time.time()</code> can step backwards. Then <code>now - updated</code> is negative, the refill drains tokens below their old value, and <code>Retry-After</code> grows ({C("ratelimit.py:14-15")}). A large forward step refills the bucket to capacity at once.</p></div>
    <div class="card callout" id="couple-xff"><h3>Header-chosen keys never expire</h3><p>Each new <code>X-Forwarded-For</code> value creates a full bucket that is never removed ({C("ratelimit.py:32")}). A client that changes the header on every request skips the limit and grows the dict.</p></div>
  </div>
</section>

<section id="docs">
  <div class="label">07 · Design docs</div>
  <h2>What the docs say</h2>
  <p class="lead">The repo has no Markdown docs. The closest things are two docstrings and a config file.</p>
  <div class="tablewrap"><table>
    <thead><tr><th>Source</th><th>Claim</th><th>Verdict</th></tr></thead>
    <tbody>
      <tr id="doc-module"><td>{C("ratelimit.py:1")}</td><td>"Per-client token buckets for POST /shorten."</td><td><span class="badge b-ok">agrees</span> <code>do_POST</code> is the only production caller {C("server.py:18")}</td></tr>
      <tr id="doc-take"><td>{C("ratelimit.py:13")}</td><td>"Spend one token; return 0 if allowed, else seconds until the next token."</td><td><span class="badge b-ok">agrees</span> {C("ratelimit.py:17-20")}</td></tr>
      <tr id="doc-config"><td>{C("config.toml:4-6")}</td><td>Section <code>[ratelimit]</code> sets <code>capacity = 5</code> and <code>refill_per_sec = 1.0</code>.</td><td><span class="badge b-warn">not read</span> Same values as {C("server.py:11")}, but no code reads <code>config.toml</code>. Editing it changes nothing.</td></tr>
    </tbody></table></div>
</section>

<section id="unconfirmed-section">
  <div class="label">08 · Unconfirmed</div>
  <h2>What the trace did not establish</h2>
  <div class="card unconfirmed"><ul class="clean">
    <li id="u1"><b>The race is read from the code, not reproduced.</b> Each request gets its own thread ({C("server.py:50")}), and no lock covers {C("ratelimit.py:15-18")}. Whether a thread switch lands between the check and the spend depends on the interpreter's switch interval.</li>
    <li id="u2"><b>Nothing in the repo calls <code>main()</code></b> ({C("server.py:49")}). There is no <code>__main__</code> block or launcher, so how the server is started is unknown.</li>
    <li id="u3"><b>Whether a trusted proxy sets <code>X-Forwarded-For</code></b> is outside the repo. Without one, the key is whatever the client sends.</li>
    <li id="u4"><b>Inferred edges:</b> none.</li>
  </ul></div>
</section>

<section id="next">
  <div class="label">09 · Left out and next</div>
  <h2>Where to look next</h2>
  <div class="grid">
    <div class="card"><h3>Left out</h3><p>How <code>LinkStore</code> computes expiry, how <code>new_code</code> picks characters, and <code>tests/test_store.py</code>. They appear only as steps on the route map.</p></div>
    <div class="card"><h3>Next</h3><p>Run <code>/analyze-code app/store.py</code> for the expiry path behind 410, or <code>/analyze-code Handler.do_POST</code> for the whole create path.</p></div>
  </div>
</section>
</main>
{{ARROW_ON_BLOCK}}
<script>{{PAGE_JS_BLOCK}}</script>
</body>
</html>
"""
page = (page.replace("{EDGES}", str(EDGES["solid"])).replace("{INFERRED}", str(EDGES["inferred"]))
        .replace("{FIG_CSS_BLOCK}", FIG_CSS).replace("{ARROW_ON_BLOCK}", ARROW_ON).replace("{PAGE_JS_BLOCK}", PAGE_JS))
open(OUT, "w").write(page)
report()
