#!/usr/bin/env python3
"""Check text against the Simplified Technical English (ASD-STE100) rules that a script can check.

  ste_check.py summary.md               # Markdown (chosen by extension; --kind overrides)
  ste_check.py page.body.html           # HTML: text of p, li, dd, dt, td and figcaption
  ste_check.py - < reply.md             # standard input, read as Markdown
  ste_check.py --format json notes.md   # one object with every warning
  ste_check.py --strict notes.md        # exit 1 when there is a warning

The checker skips code blocks, inline code, URLs, tags and text between <!-- ste: off --> and <!-- ste: on -->.
It treats inline code as one technical name, and a quoted word as a mention, not a use.
It prints warnings, not errors: it cannot judge meaning, so STE.md (next to this folder) tells the writer how
to read each one. Other scripts import check() and summary_lines() to warn about the text they write.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from pathlib import Path

MAX_DESCRIPTIVE = 25
MAX_INSTRUCTION = 20
MAX_PARAGRAPH = 6
CODE = "CODE"  # what inline code becomes: one word, a technical name

IMPERATIVES = set("""
add answer ask attach avoid build call change check choose click close copy create delete do don't edit end enter
find fix follow give install keep let link list make mark move name never open pass pick prefer press print put read
record register reload remove rerun replace restart return run save see select send set show skip start stop take
tell treat try type use wait write
""".split())
CONDITIONS = ("if ", "when ", "before ", "after ", "once ", "unless ", "until ")
PARTICIPLES = set("""
made done built sent kept held shown written given taken found left lost known seen told thrown chosen broken spent
set put cut bound brought bought caught taught thought sold paid laid led fed met won driven hidden forgotten gotten
begun drawn grown worn torn frozen spoken stolen beaten eaten fallen shaken mistaken forbidden overwritten rewritten
rebuilt split shut spread stuck struck hung dealt felt heard meant sought swept wound understood withheld upheld
""".split())
NOT_PARTICIPLES = {"need", "needed", "speed", "feed", "seed", "bed", "red", "shed", "bleed", "breed", "embed", "proceed",
                   "exceed", "succeed", "indeed", "weed", "deed", "reed", "creed", "greed"}
ING_OK = set("""
thing things something nothing anything everything string strings during morning evening ceiling sibling siblings
warning warnings heading headings setting settings binding bindings mapping mappings padding spring bring sting
swing wing wings king ring rings sing ping ding wring meaning meanings
logging routing caching hashing polling scheduling rendering staging
""".split())
PHRASAL = {
    "set up": "start, create or prepare", "carry out": "do", "find out": "find or learn", "look into": "examine",
    "figure out": "find", "point out": "show or tell", "end up": "become or stop", "come up with": "make or find",
    "go through": "examine or read", "fill in": "complete", "fill out": "complete", "pick up": "get or take",
    "turn on": "start", "turn off": "stop", "take out": "remove", "put in": "add", "clean up": "remove or clean",
    "run into": "find or get", "give up": "stop",
}
WORDS = {
    "utilize": "use", "utilise": "use", "utilization": "use", "leverage": "use", "facilitate": "help",
    "numerous": "many", "approximately": "about", "commence": "start", "terminate": "stop or end",
    "sufficient": "enough", "prior to": "before", "in order to": "to", "subsequently": "then or after",
    "additional": "more", "obtain": "get", "ensure": "make sure", "ensures": "makes sure", "perform": "do",
    "performs": "does", "via": "through or with", "e.g.": "for example", "i.e.": "that is",
    "etc.": "the items themselves", "respectively": "each pair, stated", "aforementioned": "this",
    "therefore": "so", "however": "but", "whereas": "but", "thereby": "so", "in the event that": "if",
    "a number of": "some or many", "with regard to": "about", "regarding": "about", "demonstrate": "show",
    "demonstrates": "shows", "indicate": "show", "indicates": "shows", "initiate": "start", "initiates": "starts",
    "modify": "change", "modifies": "changes",
}
ABBREVIATIONS = {"e.g.": "eg_", "i.e.": "ie_", "etc.": "etc_", "vs.": "vs_", "approx.": "approx_"}


@dataclass
class Note:
    rule: str
    line: int
    detail: str
    sentence: str


@dataclass
class Report:
    sentences: int = 0
    notes: list[Note] = field(default_factory=list)


# ------------------------------------------------------------------ extraction
def clean(text: str) -> str:
    """One line of Markdown or HTML text with code, links, URLs, tags and emphasis made plain."""
    text = re.sub(r"`+[^`]*`+", CODE, text)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<https?://[^>]+>|https?://\S+", "URL", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\*\*|__|(?<!\w)[*_](?=\S)|(?<=\S)[*_](?!\w)", "", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def markdown_units(text: str) -> list[tuple[int, str]]:
    """(first line, text) for each paragraph and each list item, skipping code, headings, tables and HTML blocks."""
    units: list[tuple[int, str]] = []
    cur: list[str] = []
    start = 0
    fence = None

    def flush():
        nonlocal cur
        if cur:
            units.append((start, "\n".join(cur)))
        cur = []

    off = False  # inside <!-- ste: off --> ... <!-- ste: on -->, for a deliberate counter-example
    for i, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if re.fullmatch(r"<!--\s*ste:\s*(off|on)\s*-->", line):
            flush()
            off = line.split(":")[1].strip().startswith("off")
            continue
        if off:
            continue
        m = re.match(r"^(`{3,}|~{3,})", line)
        if fence:
            if m and line.startswith(fence):
                fence = None
            continue
        if m:
            flush()
            fence = m.group(1)
            continue
        if not line or line.startswith(("#", "|", "<", "---")):
            flush()
            continue
        line = re.sub(r"^>\s?", "", line)
        item = re.match(r"^([-*+]|\d+[.)])\s+(.*)$", line)
        if item:
            flush()
            start = i
            body = re.sub(r"^\*\*[^*]{1,30}\*\*\s*:\s*", "", item.group(2))  # a story label: **Risk**:
            cur = [body]
            continue
        if not cur:
            start = i
        cur.append(line)
    flush()
    return [(n, clean(t)) for n, t in units if clean(t)]


class _HTMLText(HTMLParser):
    BLOCKS = {"p", "li", "dd", "dt", "td", "figcaption"}
    SKIP = {"pre", "script", "style", "svg", "math"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.units: list[tuple[int, str]] = []
        self.depth = 0          # inside a skipped element
        self.block: list[str] | None = None
        self.line = 0
        self.code = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.depth += 1
        elif tag == "code" and self.block is not None:
            self.code += 1
            if self.code == 1:
                self.block.append(f" {CODE} ")
        elif tag in self.BLOCKS and not self.depth:
            self._flush()
            self.block, self.line = [], self.getpos()[0]
        elif tag == "a" and self.block is not None and dict(attrs).get("data-loc"):
            self.block.append(f" {CODE} ")  # an editor link renders as its path:line

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.depth = max(0, self.depth - 1)
        elif tag == "code":
            self.code = max(0, self.code - 1)
        elif tag in self.BLOCKS:
            self._flush()

    def handle_data(self, data):
        if self.block is not None and not self.depth and not self.code:
            self.block.append(data)

    def _flush(self):
        if self.block is not None:
            text = clean(" ".join(self.block))
            if text:
                self.units.append((self.line, text))
        self.block = None


def html_units(text: str) -> list[tuple[int, str]]:
    p = _HTMLText()
    p.feed(text)
    p.close()
    p._flush()
    return p.units


# ------------------------------------------------------------------ sentences
def sentences(unit: str) -> list[tuple[int, str]]:
    """(line offset, sentence) inside one paragraph."""
    protected = unit
    for abbr, mark in ABBREVIATIONS.items():
        protected = re.sub(re.escape(abbr), mark, protected, flags=re.I)
    out, pos = [], 0
    for m in re.finditer(r"(?<=[.!?])[\"')\]]?\s+(?=[A-Z0-9\"'(\[`]|" + CODE + ")", protected):
        out.append((protected[:pos].count("\n"), protected[pos:m.start()].strip()))
        pos = m.end()
    out.append((protected[:pos].count("\n"), protected[pos:].strip()))
    restore = {v: k for k, v in ABBREVIATIONS.items()}
    result = []
    for off, s in out:
        for mark, abbr in restore.items():
            s = s.replace(mark, abbr)
        s = re.sub(r"\s+", " ", s)
        if len(words(s)) >= 3:  # captions and labels are not sentences
            result.append((off, s))
    return result


def words(sentence: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9][\w'’/.-]*[A-Za-z0-9]|[A-Za-z0-9]", sentence)


def is_instruction(sentence: str) -> bool:
    s = sentence.lower()
    if s.startswith(CONDITIONS) and "," in s:
        s = s.split(",", 1)[1].strip()
    first = re.match(r"[a-z']+", s)
    return bool(first) and (first.group(0) in IMPERATIVES or s.startswith("do not"))


# ------------------------------------------------------------------ rules
def check_sentence(s: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    n = len(words(s))
    instruction = is_instruction(s)
    limit = MAX_INSTRUCTION if instruction else MAX_DESCRIPTIVE
    if n > limit:
        found.append(("length", f"{n} words (max {limit} for {'an instruction' if instruction else 'a description'})"))
    # a quoted word is a mention ("use", not "utilize"), not a use: only the length rule counts it
    plain = re.sub(r"\"[^\"]*\"|“[^”]*”", " ", s.replace(CODE, " "))
    low = plain.lower()
    for m in re.finditer(r"\b(am|is|are|was|were|be|been|being|gets?|got)\s+(?:(?:not|never|also|only|now|still|already|"
                         r"always|then|all|both|\w+ly)\s+)?([a-z]+)\b", low):
        w = m.group(2)
        if w in PARTICIPLES or (w.endswith("ed") and len(w) > 4 and w not in NOT_PARTICIPLES):
            found.append(("passive", f"\"{m.group(0)}\": name who does it, in the active voice"))
            break
    m = re.search(r"\b(has|have|had|having)\s+(?:(?:not|never|already|also|just|since)\s+)?(been|[a-z]+ed|"
                  + "|".join(sorted(PARTICIPLES)) + r")\b", low)
    if m and m.group(2) not in NOT_PARTICIPLES:
        found.append(("tense", f"\"{m.group(0)}\": use a simple tense"))
    m = re.search(r"\b(would|might|should|could)\b", low)
    if m:
        found.append(("hedge", f"\"{m.group(1)}\": state the fact or the rule with a direct verb (can, must, do)"))
    for tok in re.finditer(r"(?<![\w`/.-])([A-Za-z]+ing)s?\b", plain):
        w = tok.group(1)
        if w.lower() in ING_OK or len(w) <= 5 or (w[0].isupper() and tok.start() > 0):
            continue
        found.append(("ing", f"\"{w}\": use a verb or a noun, not an -ing form (keep it if it is a technical name)"))
        break
    for phrase, alt in PHRASAL.items():
        if re.search(r"\b" + phrase.replace(" ", r"\s+") + r"\b", low):
            found.append(("phrasal", f"\"{phrase}\": use {alt}"))
            break
    for word, alt in WORDS.items():
        if re.search(r"(?<![\w-])" + re.escape(word) + r"(?![\w-])", low):
            found.append(("word", f"\"{word}\": use {alt}"))
    if ";" in plain:
        found.append(("semicolon", "split the sentence at the semicolon"))
    if instruction and re.search(r"(,\s*|\s)(then|and then)\s+[a-z]|\band\s+(" + "|".join(sorted(IMPERATIVES)) + r")\b", low):
        found.append(("instructions", "give one instruction in each sentence"))
    return found


def check(text: str, kind: str = "markdown") -> Report:
    units = html_units(text) if kind == "html" else markdown_units(text)
    rep = Report()
    for line, unit in units:
        sents = sentences(unit)
        rep.sentences += len(sents)
        if len(sents) > MAX_PARAGRAPH:
            rep.notes.append(Note("paragraph", line, f"{len(sents)} sentences (max {MAX_PARAGRAPH})", sents[0][1]))
        for off, s in sents:
            for rule, detail in check_sentence(s):
                rep.notes.append(Note(rule, line + off, detail, s))
    return rep


def kind_of(path: str) -> str:
    return "html" if path.lower().endswith((".html", ".htm")) else "markdown"


def snippet(s: str, n: int = 90) -> str:
    s = s.replace(CODE, "`…`")
    return s if len(s) <= n else s[: n - 1] + "…"


def summary_lines(rep: Report, label: str, limit: int | None = 20) -> list[str]:
    """Human-readable warning lines, for this CLI and for scripts that warn about the text they write."""
    if not rep.notes:
        return [f"ste: no warnings in {rep.sentences} sentences ({label})"]
    out = [f"ste: {len(rep.notes)} warning{'s' if len(rep.notes) != 1 else ''} in {rep.sentences} sentences ({label}); "
           f"STE.md explains each rule"]
    shown = rep.notes if limit is None else rep.notes[:limit]
    out += [f"  L{n.line} {n.rule}: {n.detail} - \"{snippet(n.sentence)}\"" for n in shown]
    if limit is not None and len(rep.notes) > limit:
        out.append(f"  ... {len(rep.notes) - limit} more (run ste_check.py on the file for all)")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", help="Markdown, HTML or text files, or - for standard input")
    ap.add_argument("--kind", choices=["markdown", "html"], help="override the kind the extension implies")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    ap.add_argument("--strict", action="store_true", help="exit 1 when there is a warning")
    ap.add_argument("--all", action="store_true", help="print every warning, not the first 20")
    a = ap.parse_args(argv)
    total, out = 0, []
    for f in a.files:
        text = sys.stdin.read() if f == "-" else Path(f).read_text(encoding="utf-8")
        rep = check(text, a.kind or kind_of(f))
        total += len(rep.notes)
        if a.format == "json":
            out.append({"file": f, "sentences": rep.sentences, "warnings": [asdict(n) for n in rep.notes]})
        else:
            print("\n".join(summary_lines(rep, "stdin" if f == "-" else f, None if a.all else 20)))
    if a.format == "json":
        print(json.dumps(out if len(out) > 1 else out[0], indent=1))
    return 1 if a.strict and total else 0


if __name__ == "__main__":
    sys.exit(main())
