/*
 * ASCII particle engine for implementation-summary pages.
 *
 * It animates the same plain-text frames that appear in the terminal summary:
 *   - every <pre class="scene"> first assembles out of drifting particles,
 *   - box-drawing paths carry particle streams toward arrowheads (▶ ◀ ▲ ▼),
 *   - ✦ / ✧ markers emit spark bursts into empty space,
 *   - dust glyphs (· ˙ ° ⋅) twinkle and shed falling motes,
 *   - ░ ▒ cells dissolve and re-form, ╳ breaks flicker,
 *   - a .hero gets an ambient ASCII field behind its content.
 * Click a scene to replay its assembly. prefers-reduced-motion renders static frames.
 */
(() => {
  "use strict";

  const REDUCED = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const FPS = 30;

  // Direction vectors [dr, dc].
  const D = { N: [-1, 0], S: [1, 0], E: [0, 1], W: [0, -1], NE: [-1, 1], NW: [-1, -1], SE: [1, 1], SW: [1, -1] };
  const ALL8 = Object.keys(D);
  const OPP = { N: "S", S: "N", E: "W", W: "E", NE: "SW", SW: "NE", NW: "SE", SE: "NW" };

  // Which directions each path glyph connects to.
  const CONNECT = {};
  const put = (chars, dirs) => { for (const ch of chars) CONNECT[ch] = dirs; };
  put("─━┄┅┈┉╌╍═", ["E", "W"]);
  put("│┃┆┇┊┋╎║", ["N", "S"]);
  put("╱", ["NE", "SW"]);
  put("╲", ["NW", "SE"]);
  put("╭┌╔", ["E", "S"]);
  put("╮┐╗", ["W", "S"]);
  put("╰└╚", ["E", "N"]);
  put("╯┘╝", ["W", "N"]);
  put("├╠", ["N", "S", "E"]);
  put("┤╣", ["N", "S", "W"]);
  put("┬╦", ["E", "W", "S"]);
  put("┴╩", ["E", "W", "N"]);
  put("┼╬", ["N", "S", "E", "W"]);
  put("∙", ALL8);
  // Arrowheads are sinks and only accept streams from behind them.
  const HEADS = { "▶": ["W", "NW", "SW"], "►": ["W", "NW", "SW"], "◀": ["E", "NE", "SE"], "◄": ["E", "NE", "SE"], "▼": ["N", "NW", "NE"], "▲": ["S", "SW", "SE"] };
  Object.assign(CONNECT, HEADS);

  const MARKS = new Set(["✦", "✧", "⋆"]);
  const DUST = new Set(["·", "˙", "°", "⋅"]);
  const FADE = new Set(["░", "▒"]);
  const BREAK = "╳";
  const NOISE = ["·", "∙", "˙", "+", "*", "∘"];
  const SPARK_BY_LIFE = ["˙", "·", "+", "*"];

  const rand = (a, b) => a + Math.random() * (b - a);
  const pick = (arr) => arr[(Math.random() * arr.length) | 0];
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const easeOut = (t) => 1 - Math.pow(1 - t, 3);

  function classify(ch) {
    if (ch === " ") return "";
    if (HEADS[ch]) return "g-head";
    if (CONNECT[ch]) return "g-path";
    if (MARKS.has(ch)) return "g-mark";
    if (DUST.has(ch)) return "g-dust";
    if (FADE.has(ch)) return "g-fade";
    if (ch === BREAK) return "g-break";
    return "g-text";
  }

  /* ------------------------------------------------------------------ scene */

  class Scene {
    constructor(pre) {
      this.pre = pre;
      const text = pre.textContent.replace(/^\n/, "").replace(/\s+$/, "");
      this.rows = text.split("\n").map((line) => Array.from(line));
      this.h = this.rows.length;
      this.w = Math.max(...this.rows.map((r) => r.length), 1);
      for (const r of this.rows) while (r.length < this.w) r.push(" ");
      this.calm = pre.hasAttribute("data-calm");
      this.particles = [];
      this.sparks = [];
      this.motes = [];
      this.visible = false;
      this.assembleStart = null;
      this.lastSpawn = new Map();
      this.buildGraph();
      this.collectCells();
      this.phase = this.rows.map((r) => r.map(() => Math.random() * Math.PI * 2));
      pre.setAttribute("aria-label", text);
      pre.addEventListener("click", () => this.replay());
      this.renderStatic();
    }

    at(r, c) {
      return r >= 0 && r < this.h && c >= 0 && c < this.w ? this.rows[r][c] : " ";
    }

    // Build an undirected graph of path cells. Two neighbouring cells connect when either
    // glyph claims the shared direction, except arrowheads, which must claim it themselves.
    buildGraph() {
      this.adj = new Map();
      const key = (r, c) => r * 10000 + c;
      this.key = key;
      for (let r = 0; r < this.h; r++) {
        for (let c = 0; c < this.w; c++) {
          const ch = this.rows[r][c];
          if (!CONNECT[ch]) continue;
          const list = [];
          for (const d of ALL8) {
            const [dr, dc] = D[d];
            const nr = r + dr, nc = c + dc;
            const nch = this.at(nr, nc);
            if (!CONNECT[nch]) continue;
            const mine = CONNECT[ch].includes(d);
            const theirs = CONNECT[nch].includes(OPP[d]);
            const ok = HEADS[ch] ? mine : HEADS[nch] ? theirs : mine || theirs;
            if (ok) list.push(key(nr, nc));
          }
          this.adj.set(key(r, c), list);
        }
      }
      // Distance to the nearest arrowhead, by BFS from every head.
      this.dist = new Map();
      const queue = [];
      for (const [k] of this.adj) {
        const r = Math.floor(k / 10000), c = k % 10000;
        if (HEADS[this.rows[r][c]]) { this.dist.set(k, 0); queue.push(k); }
      }
      for (let i = 0; i < queue.length; i++) {
        const k = queue[i];
        for (const n of this.adj.get(k)) {
          if (!this.dist.has(n)) { this.dist.set(n, this.dist.get(k) + 1); queue.push(n); }
        }
      }
      // Sources: reachable cells with no neighbour farther from a head.
      this.sources = [];
      for (const [k, d] of this.dist) {
        if (d === 0) continue;
        if (this.adj.get(k).every((n) => (this.dist.get(n) ?? -1) <= d)) this.sources.push(k);
      }
    }

    collectCells() {
      this.marks = [];
      this.dust = [];
      this.cells = [];
      for (let r = 0; r < this.h; r++) {
        for (let c = 0; c < this.w; c++) {
          const ch = this.rows[r][c];
          if (ch === " ") continue;
          this.cells.push({ r, c, ch, sr: rand(-6, this.h + 6), sc: rand(-12, this.w + 12), delay: (c / this.w) * 0.45 + rand(0, 0.25) });
          if (MARKS.has(ch)) this.marks.push({ r, c, next: rand(0.4, 2.5) });
          if (DUST.has(ch)) this.dust.push({ r, c });
        }
      }
    }

    replay() {
      if (REDUCED) return;
      this.assembleStart = null;
      this.particles = [];
      this.sparks = [];
      this.motes = [];
    }

    // Base layer: glyph + class per cell, with time-varying classes for dust/fade/break.
    baseLayer(t) {
      const glyph = this.rows.map((r) => r.slice());
      const cls = this.rows.map((r) => r.map(classify));
      if (REDUCED || t == null) return { glyph, cls };
      for (let r = 0; r < this.h; r++) {
        for (let c = 0; c < this.w; c++) {
          const ch = glyph[r][c];
          if (DUST.has(ch)) {
            const v = Math.sin(t * 1.7 + this.phase[r][c]);
            cls[r][c] = v > 0.55 ? "g-dust g-hi" : v < -0.5 ? "g-dust g-lo" : "g-dust";
          } else if (FADE.has(ch)) {
            const v = Math.sin(t * 0.9 + this.phase[r][c] * 3);
            if (v > 0.75) glyph[r][c] = " ";
            else if (v > 0.35) glyph[r][c] = "░";
          } else if (ch === BREAK) {
            cls[r][c] = Math.sin(t * 6 + this.phase[r][c]) > 0.6 ? "g-break g-lo" : "g-break";
          } else if (MARKS.has(ch)) {
            const v = Math.sin(t * 2.3 + this.phase[r][c]);
            cls[r][c] = v > 0.6 ? "g-mark g-hi" : "g-mark";
          }
        }
      }
      return { glyph, cls };
    }

    step(t, dt) {
      if (this.calm) return;
      // Spawn stream particles from sources, staggered and capped.
      if (this.particles.length < 48) {
        for (const s of this.sources) {
          const next = this.lastSpawn.get(s) ?? t + rand(0, 1.5);
          if (t >= next) {
            this.particles.push({ k: s, prev: [], acc: 0, speed: rand(11, 17) });
            this.lastSpawn.set(s, t + rand(0.9, 2.2));
          } else if (!this.lastSpawn.has(s)) {
            this.lastSpawn.set(s, next);
          }
        }
      }
      for (const p of this.particles) {
        p.acc += dt * p.speed;
        while (p.acc >= 1 && !p.done) {
          p.acc -= 1;
          const d = this.dist.get(p.k);
          if (d === 0) { p.done = true; p.flash = 0.25; break; }
          const options = this.adj.get(p.k).filter((n) => this.dist.get(n) === d - 1);
          if (!options.length) { p.done = true; break; }
          p.prev.unshift(p.k);
          if (p.prev.length > 3) p.prev.pop();
          p.k = pick(options);
        }
        if (p.done && p.flash != null) p.flash -= dt;
      }
      this.particles = this.particles.filter((p) => !p.done || (p.flash != null && p.flash > 0));

      // Marker bursts.
      for (const m of this.marks) {
        m.next -= dt;
        if (m.next <= 0) {
          m.next = rand(2.2, 4.5);
          const n = (rand(6, 11)) | 0;
          for (let i = 0; i < n; i++) {
            const a = rand(0, Math.PI * 2), v = rand(4, 9);
            this.sparks.push({ r: m.r, c: m.c, vr: Math.sin(a) * v * 0.5, vc: Math.cos(a) * v, life: rand(0.6, 1.1), max: 1.1 });
          }
        }
      }
      for (const s of this.sparks) { s.r += s.vr * dt; s.c += s.vc * dt; s.vr *= 0.96; s.vc *= 0.96; s.life -= dt; }
      this.sparks = this.sparks.filter((s) => s.life > 0);

      // Dust motes drift down and fade.
      for (const d of this.dust) {
        if (Math.random() < dt * 0.06) this.motes.push({ r: d.r + 1, c: d.c + rand(-0.4, 0.4), life: rand(1.2, 2.4) });
      }
      for (const m of this.motes) { m.r += dt * 0.9; m.life -= dt; }
      this.motes = this.motes.filter((m) => m.life > 0 && m.r < this.h);
    }

    frame(t, dt) {
      if (this.assembleStart == null) this.assembleStart = t;
      const at = t - this.assembleStart;
      const dur = 1.25;
      if (at < dur + 0.5) return this.renderAssemble(at / dur);
      this.step(t, dt);
      const { glyph, cls } = this.baseLayer(t);
      const blank = (r, c) => r >= 0 && r < this.h && c >= 0 && c < this.w && glyph[r][c] === " ";
      for (const m of this.motes) {
        const r = Math.round(m.r), c = Math.round(m.c);
        if (blank(r, c)) { glyph[r][c] = m.life > 0.8 ? "˙" : "."; cls[r][c] = "g-mote"; }
      }
      for (const s of this.sparks) {
        const r = Math.round(s.r), c = Math.round(s.c);
        if (blank(r, c)) {
          glyph[r][c] = SPARK_BY_LIFE[Math.min(3, Math.floor((s.life / s.max) * 4))];
          cls[r][c] = s.life > 0.55 ? "g-spark g-hi" : "g-spark";
        }
      }
      for (const p of this.particles) {
        p.prev.forEach((k, i) => {
          const r = Math.floor(k / 10000), c = k % 10000;
          cls[r][c] = i === 0 ? "g-trail g-hi" : "g-trail";
        });
        const r = Math.floor(p.k / 10000), c = p.k % 10000;
        if (p.done) { cls[r][c] = "g-head g-flash"; continue; }
        if (!HEADS[glyph[r][c]]) glyph[r][c] = "•";
        cls[r][c] = "g-particle";
      }
      this.paint(glyph, cls);
    }

    renderAssemble(p) {
      const glyph = this.rows.map((r) => r.map(() => " "));
      const cls = this.rows.map((r) => r.map(() => ""));
      for (const cell of this.cells) {
        const local = Math.max(0, Math.min(1, (p - cell.delay) / 0.75));
        const e = easeOut(local);
        const r = Math.round(cell.sr + (cell.r - cell.sr) * e);
        const c = Math.round(cell.sc + (cell.c - cell.sc) * e);
        if (r < 0 || r >= this.h || c < 0 || c >= this.w) continue;
        if (local >= 1) { glyph[r][c] = cell.ch; cls[r][c] = classify(cell.ch); }
        else if (glyph[r][c] === " ") { glyph[r][c] = local > 0.7 ? cell.ch : pick(NOISE); cls[r][c] = "g-spark" + (local > 0.4 ? " g-hi" : ""); }
      }
      this.paint(glyph, cls);
    }

    renderStatic() {
      const { glyph, cls } = this.baseLayer(null);
      this.paint(glyph, cls);
    }

    paint(glyph, cls) {
      let out = "";
      for (let r = 0; r < this.h; r++) {
        let run = "", runCls = null;
        for (let c = 0; c < this.w; c++) {
          const k = cls[r][c];
          if (k !== runCls) {
            if (run) out += runCls ? `<span class="${runCls}">${esc(run)}</span>` : esc(run);
            run = ""; runCls = k;
          }
          run += glyph[r][c];
        }
        if (run) out += runCls ? `<span class="${runCls}">${esc(run)}</span>` : esc(run);
        if (r < this.h - 1) out += "\n";
      }
      this.pre.innerHTML = out;
    }
  }

  /* ------------------------------------------------------------- hero field */

  class Field {
    constructor(host) {
      this.host = host;
      this.pre = document.createElement("pre");
      this.pre.className = "field";
      this.pre.setAttribute("aria-hidden", "true");
      host.prepend(this.pre);
      this.mouse = { x: -1e4, y: -1e4 };
      host.addEventListener("pointermove", (e) => {
        const b = this.pre.getBoundingClientRect();
        this.mouse = { x: (e.clientX - b.left) / this.cw, y: (e.clientY - b.top) / this.ch };
      });
      host.addEventListener("pointerleave", () => { this.mouse = { x: -1e4, y: -1e4 }; });
      this.visible = true;
      this.measure();
      window.addEventListener("resize", () => this.measure());
    }

    measure() {
      const probe = document.createElement("span");
      probe.textContent = "M".repeat(20);
      this.pre.textContent = "";
      this.pre.appendChild(probe);
      const b = probe.getBoundingClientRect();
      this.cw = b.width / 20 || 8;
      this.ch = b.height || 16;
      const box = this.host.getBoundingClientRect();
      this.cols = Math.ceil(box.width / this.cw) + 1;
      this.rowsN = Math.ceil(box.height / this.ch) + 1;
      this.render(REDUCED ? 3.7 : performance.now() / 1000);
    }

    render(t) {
      const ramp = " .·:-=+*";
      let out = "";
      for (let y = 0; y < this.rowsN; y++) {
        let line = "";
        for (let x = 0; x < this.cols; x++) {
          let v = Math.sin(x * 0.11 + t * 0.6) + Math.sin(y * 0.27 - t * 0.45) + Math.sin((x + y) * 0.07 + t * 0.3) + Math.sin(Math.hypot(x - this.cols * 0.7, (y - this.rowsN * 0.4) * 2) * 0.16 - t);
          v = (v + 4) / 8;
          const dx = x - this.mouse.x, dy = (y - this.mouse.y) * 2;
          const near = Math.exp(-(dx * dx + dy * dy) / 90);
          v = v * v * 0.9 + near * 0.6;
          // Fade the field toward the left so it stays behind the headline.
          v *= 0.25 + 0.75 * (x / this.cols);
          line += ramp[Math.max(0, Math.min(ramp.length - 1, Math.floor(v * ramp.length)))];
        }
        out += line + "\n";
      }
      this.pre.textContent = out;
    }
  }

  /* ------------------------------------------------------------------- loop */

  function boot() {
    const scenes = [...document.querySelectorAll("pre.scene")].map((pre) => new Scene(pre));
    const fields = [...document.querySelectorAll(".hero")].map((h) => new Field(h));
    if (REDUCED) return;

    const io = new IntersectionObserver((entries) => {
      for (const e of entries) {
        const s = scenes.find((x) => x.pre === e.target) || fields.find((f) => f.host === e.target);
        if (s) s.visible = e.isIntersecting;
      }
    }, { threshold: 0.15 });
    scenes.forEach((s) => io.observe(s.pre));
    fields.forEach((f) => io.observe(f.host));

    let last = performance.now() / 1000, acc = 0;
    const tick = (ms) => {
      const t = ms / 1000;
      const dt = Math.min(0.1, t - last);
      last = t;
      acc += dt;
      if (acc >= 1 / FPS) {
        for (const s of scenes) if (s.visible) s.frame(t, acc);
        for (const f of fields) if (f.visible) f.render(t);
        acc = 0;
      }
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
