"""GitHub pull requests as review sessions: resolve a PR link, materialize its head, post backlog as a review.

`open --pr <link>` reviews a PR without touching the user's working tree:
- the PR's head is fetched (`refs/pull/N/head`, which also covers fork PRs) into a local clone of the
  base repository, the one in the current directory when one of its remotes is that repository,
  otherwise a blobless clone cached under <home>/clones;
- it is checked out detached in its own worktree (<home>/worktrees/<owner>-<repo>-pr<N>), which becomes
  the session's repo, so the editor, search and the reviewer read the PR's real files;
- the diff is taken against the merge-base with the base branch, which is what GitHub shows.

Reopening the same link reuses the session and moves a clean worktree to the latest head.
`pr <sid> post` turns backlog items into one GitHub review: an inline comment where the item's anchor
falls inside a diff hunk of the PR (head vs merge-base, ignoring local edits), the review body otherwise.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import gitdiff
import store
from axi import DeskError, run

URL_RE = re.compile(r"^(?:https?://)?(?P<host>[^/\s]+)/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+?)(?:\.git)?/pulls?/(?P<n>\d+)(?:[/?#].*)?$")
SHORT_RE = re.compile(r"^(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)#(?P<n>\d+)$")
BODY_LIMIT = 8000
COMMENT_LIMIT = 1500
DISCUSSION_LIMIT = 60
VIEW_FIELDS = ("number,title,body,url,state,isDraft,author,baseRefName,baseRefOid,headRefName,headRefOid,"
               "headRepository,headRepositoryOwner,additions,deletions,changedFiles,comments,reviews")


def sh(*args: str, cwd: Path | None = None, input: str | None = None) -> str:
    try:
        return subprocess.run(list(args), cwd=cwd, input=input, capture_output=True, text=True, check=True).stdout
    except FileNotFoundError:
        raise DeskError(f"{args[0]} is not installed", ["Install the GitHub CLI (`brew install gh`), then `gh auth login`"])
    except subprocess.CalledProcessError as e:
        msg = (e.stderr or e.stdout or "").strip().splitlines()
        raise DeskError(f"`{' '.join(args[:3])} ...` failed: {msg[-1] if msg else e.returncode}",
                        ["Check `gh auth status` and that the PR link is right"])


def git(repo: Path, *args: str) -> str:
    return sh("git", "-C", str(repo), *args)


def parse_ref(ref: str, cwd: Path) -> dict:
    """A PR link (https://github.com/o/r/pull/12, with any /files or #fragment), `o/r#12`, or a bare
    number for the repository in the current directory."""
    ref = ref.strip()
    if m := URL_RE.match(ref):
        return {"host": m["host"].lower(), "owner": m["owner"], "repo": m["repo"], "number": int(m["n"])}
    if m := SHORT_RE.match(ref):
        return {"host": "github.com", "owner": m["owner"], "repo": m["repo"], "number": int(m["n"])}
    if ref.lstrip("#").isdigit():
        url = json.loads(sh("gh", "pr", "view", ref.lstrip("#"), "--json", "url", cwd=cwd))["url"]
        return parse_ref(url, cwd)
    raise DeskError(f"not a pull request link: {ref}", ["Pass --pr https://github.com/<owner>/<repo>/pull/<n> (or <owner>/<repo>#<n>)"], 2)


def slug(pr: dict) -> str:
    """The -R value gh takes: OWNER/REPO on github.com, HOST/OWNER/REPO elsewhere."""
    base = f"{pr['owner']}/{pr['repo']}"
    return base if pr["host"] == "github.com" else f"{pr['host']}/{base}"


def api(pr: dict, path: str, *args: str, input: str | None = None) -> str:
    return sh("gh", "api", "--hostname", pr["host"], path, *args, input=input)


def fetch_meta(pr: dict) -> dict:
    view = json.loads(sh("gh", "pr", "view", str(pr["number"]), "-R", slug(pr), "--json", VIEW_FIELDS))
    out = api(pr, f"repos/{pr['owner']}/{pr['repo']}/pulls/{pr['number']}/comments", "--paginate", "--jq", ".[]")
    view["inline"] = [json.loads(line) for line in out.splitlines() if line.strip()]
    return view


def remote_for(clone: Path, pr: dict) -> str | None:
    """The remote of `clone` that is the PR's base repository, if any."""
    want = re.compile(rf"{re.escape(pr['host'])}[:/]{re.escape(pr['owner'])}/{re.escape(pr['repo'])}(?:\.git)?/?$", re.I)
    try:  # the configured URLs: `git remote -v` shows them after url.insteadOf rewrites
        out = git(clone, "config", "--get-regexp", r"^remote\..*\.url$")
    except DeskError:
        return None
    for line in out.splitlines():
        key, url = line.split(maxsplit=1)
        if want.search(url.strip()):
            return key[len("remote."):-len(".url")]
    return None


def find_clone(pr: dict, cwd: Path) -> tuple[Path, str, bool]:
    """(clone, remote, is_users_own_checkout): the current repository when it tracks the PR's base repo,
    else a blobless clone cached under the desk home (created on first use)."""
    try:
        top = Path(git(cwd, "rev-parse", "--show-toplevel").strip())
    except DeskError:
        top = None
    if top and (remote := remote_for(top, pr)):
        return top, remote, True
    cache = store.home() / "clones" / pr["host"] / pr["owner"] / pr["repo"]
    if not (cache / ".git").exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        sh("gh", "repo", "clone", slug(pr), str(cache), "--", "--filter=blob:none", "--no-checkout", "--quiet")
    return cache, "origin", False


def materialize(pr: dict, view: dict, cwd: Path, previous_head: str | None) -> dict:
    """Fetch head and base, place the head in the PR's worktree, and return where everything is."""
    clone, remote, own = find_clone(pr, cwd)
    n = pr["number"]
    head_ref, base_ref = f"refs/review-desk/pr-{n}/head", f"refs/review-desk/pr-{n}/base"
    # The base is the PR's baseRefOid: the base branch's tip while the PR is open, the base it was merged
    # onto once merged (the branch has moved past the head by then, and may be gone). Fetched by SHA, with
    # the branch as a fallback for servers that refuse SHA wants.
    try:
        git(clone, "fetch", "--quiet", "--no-tags", remote, f"+refs/pull/{n}/head:{head_ref}", f"+{view['baseRefOid']}:{base_ref}")
    except DeskError:
        git(clone, "fetch", "--quiet", "--no-tags", remote, f"+refs/pull/{n}/head:{head_ref}", f"+refs/heads/{view['baseRefName']}:{base_ref}")
    latest = git(clone, "rev-parse", head_ref).strip()
    wt = store.home() / "worktrees" / f"{pr['owner']}-{pr['repo']}-pr{n}"
    # `head` is the commit placed in the worktree: what the user reviews, what the diff and posted comments
    # refer to. It follows new pushes unless the worktree has local work, which is never thrown away.
    head, moved = latest, "created"
    if (wt / ".git").exists():
        current = git(wt, "rev-parse", "HEAD").strip()
        placed = previous_head or current
        dirty = bool(git(wt, "status", "--porcelain").strip())
        if current == latest:
            moved = "at head"
        elif dirty or current != placed:  # edits, or commits on top of what the desk placed (an Execute)
            head = placed
            moved = f"kept at {current[:10]}: the worktree has local work; the PR head is now {latest[:10]}"
        else:
            git(wt, "checkout", "--quiet", "--detach", latest)
            moved = f"moved to {latest[:10]}"
    else:
        git(clone, "worktree", "prune")
        wt.parent.mkdir(parents=True, exist_ok=True)
        git(clone, "worktree", "add", "--quiet", "--detach", str(wt), latest)
    merge_base = git(clone, "merge-base", base_ref, head).strip()
    return {"worktree": str(wt), "clone": str(clone), "remote": remote, "home_repo": str(clone) if own else None,
            "head_sha": head, "latest_head": latest, "merge_base": merge_base, "worktree_state": moved}


def info(pr: dict, view: dict, where: dict) -> dict:
    head_repo = view.get("headRepository") or {}
    head_owner = (view.get("headRepositoryOwner") or {}).get("login")
    return {"url": view["url"], "host": pr["host"], "owner": pr["owner"], "repo": pr["repo"], "number": pr["number"],
            "title": view["title"], "author": (view.get("author") or {}).get("login"), "state": view["state"],
            "draft": view.get("isDraft", False), "base_ref": view["baseRefName"], "head_ref": view["headRefName"],
            "head_repo": f"{head_owner}/{head_repo.get('name')}" if head_owner and head_repo.get("name") else None,
            **where}


def plural(n, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{'?' if n is None else n} {word}s"


def clipped(text: str | None, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + f"\n\n… (truncated, {len(text)} chars)"


def context_md(p: dict, view: dict) -> str:
    """The PR's description and discussion, for the reviewer's context.md."""
    lines = [f"## Pull request {p['owner']}/{p['repo']}#{p['number']}: {p['title']}", "",
             f"- url: {p['url']}",
             f"- author: @{p['author']}   state: {p['state'].lower()}{' (draft)' if p['draft'] else ''}",
             f"- {p['base_ref']} <- {p['head_repo'] + ':' if p['head_repo'] and p['head_repo'] != p['owner'] + '/' + p['repo'] else ''}{p['head_ref']}"
             f"   head {p['head_sha'][:10]}   merge-base {p['merge_base'][:10]}",
             f"- {plural(view.get('changedFiles'), 'file')}, +{view.get('additions', '?')} -{view.get('deletions', '?')}",
             f"- the code under review is checked out at {p['worktree']} (the session's repo)", "",
             "### Description", "", clipped(view.get("body"), BODY_LIMIT) or "(no description)", ""]
    talk = []
    for c in view.get("comments") or []:
        talk.append((c.get("createdAt", ""), f"@{(c.get('author') or {}).get('login')} commented", c.get("body")))
    for r in view.get("reviews") or []:
        if r.get("body") or r.get("state") not in ("COMMENTED",):
            talk.append((r.get("submittedAt", ""), f"@{(r.get('author') or {}).get('login')} reviewed: {r.get('state', '').lower().replace('_', ' ')}", r.get("body")))
    for c in view.get("inline") or []:
        line = c.get("line") or c.get("original_line")
        talk.append((c.get("created_at", ""), f"@{(c.get('user') or {}).get('login')} on {c.get('path')}:{line}"
                     + (" (outdated)" if c.get("line") is None else ""), c.get("body")))
    talk.sort(key=lambda t: t[0])
    lines += [f"### Discussion ({len(talk)} entries)", ""]
    if not talk:
        lines += ["(none yet)", ""]
    for at, who, body in talk[-DISCUSSION_LIMIT:]:
        lines += [f"**{who}** ({at[:10]})", "", clipped(body, COMMENT_LIMIT) or "(no text)", ""]
    if len(talk) > DISCUSSION_LIMIT:
        lines += [f"… {len(talk) - DISCUSSION_LIMIT} earlier entries: `gh pr view {p['url']} --comments`", ""]
    return "\n".join(lines)


# --------------------------------------------------------------------- page
SPLIT = "RDSPLIT-6c1f0e"
PAGE_CSS = """
:root { --bg:#050505; --surface:#161616; --surface-2:#0d0d0d; --border:#262626; --text:#fff; --text-2:#a1a1aa; --text-3:#71717a;
  --primary:#6366f1; --pass:#34d399; --warn:#fbbf24; --fail:#f87171;
  --mono:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,monospace; --sans:"Inter",ui-sans-serif,system-ui,-apple-system,sans-serif; color-scheme:dark; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text); font:400 14px/1.6 var(--sans); -webkit-font-smoothing:antialiased; }
main { max-width:860px; margin:0 auto; padding:40px 32px 80px; }
.eyebrow { font:600 12px/1.2 var(--mono); letter-spacing:.06em; text-transform:uppercase; color:var(--text-3); display:flex; gap:10px; align-items:center; }
.eyebrow a { color:inherit; text-decoration:none; text-transform:none; letter-spacing:0; } .eyebrow a:hover { color:var(--text-2); text-decoration:underline; }
h1 { font-size:26px; line-height:1.25; font-weight:500; margin:12px 0 16px; }
.state { font:600 11px/1 var(--mono); padding:5px 10px; border-radius:9999px; border:1px solid var(--border); color:var(--text-2); }
.state.open { color:var(--pass); border-color:rgba(52,211,153,.4); } .state.merged { color:#a78bfa; border-color:rgba(167,139,250,.4); }
.state.closed { color:var(--fail); border-color:rgba(248,113,113,.4); }
.facts { display:flex; flex-wrap:wrap; gap:8px 20px; font:400 12px/1.4 var(--mono); color:var(--text-2); margin-bottom:28px; }
.facts b { color:var(--text); font-weight:600; } .add { color:var(--pass); } .del { color:var(--fail); }
.card { background:var(--surface); border:1px solid var(--border); border-radius:20px; padding:20px 24px; margin:0 0 16px; }
.card .who { font:600 12px/1.2 var(--mono); color:var(--text-2); margin-bottom:12px; display:flex; gap:8px; flex-wrap:wrap; }
.card .who .when { color:var(--text-3); font-weight:400; } .card .who .where { color:var(--primary); font-weight:400; }
h2 { font:600 12px/1.2 var(--mono); letter-spacing:.06em; text-transform:uppercase; color:var(--text-3); margin:36px 0 14px; }
.md > :first-child { margin-top:0; } .md > :last-child { margin-bottom:0; }
.md h1,.md h2,.md h3,.md h4 { font-family:var(--sans); text-transform:none; letter-spacing:0; color:var(--text); font-weight:600; margin:20px 0 8px; }
.md h1 { font-size:20px; } .md h2 { font-size:17px; } .md h3,.md h4 { font-size:15px; }
.md p, .md ul, .md ol { margin:0 0 12px; } .md li { margin:2px 0; } .md li > p { margin:0; }
.md a { color:var(--primary); text-decoration:none; } .md a:hover { text-decoration:underline; }
.md code { font:12.5px/1.5 var(--mono); background:var(--surface-2); border:1px solid var(--border); border-radius:6px; padding:1px 5px; }
.md pre { background:var(--surface-2); border:1px solid var(--border); border-radius:12px; padding:14px 16px; overflow:auto; }
.md pre code { border:0; padding:0; background:none; }
.md blockquote { margin:0 0 12px; padding:2px 14px; border-left:2px solid var(--border); color:var(--text-2); }
.md table { border-collapse:collapse; margin:0 0 12px; display:block; overflow:auto; }
.md th, .md td { border:1px solid var(--border); padding:6px 10px; }
.md img { max-width:100%; border-radius:8px; } .md hr { border:0; border-top:1px solid var(--border); margin:16px 0; }
.md input[type=checkbox] { margin:0 6px 0 -18px; accent-color:var(--primary); } .md .contains-task-list { list-style:none; padding-left:22px; }
.empty { color:var(--text-3); font-style:italic; }
"""


def render_markdown(pr: dict, texts: list[str]) -> list[str]:
    """GitHub's own GFM rendering (sanitized, with @mentions and #refs linked), one API call for every text;
    escaped plain text when the API is out of reach."""
    import html as H
    if not texts:
        return []
    joined = f"\n\n{SPLIT}\n\n".join(t.strip() or "_(no text)_" for t in texts)
    try:
        out = api(pr, "markdown", "-f", f"text={joined}", "-f", "mode=gfm", "-f", f"context={pr['owner']}/{pr['repo']}")
        parts = re.split(rf"<p[^>]*>\s*{SPLIT}\s*</p>", out)  # GitHub writes <p dir="auto">
        if len(parts) == len(texts):
            return parts
    except DeskError:
        pass
    return [f"<pre style='white-space:pre-wrap'>{H.escape(t.strip())}</pre>" for t in texts]


def page_html(p: dict, view: dict) -> str:
    """The PR as a page: description, then the discussion, in the desk's look. It renders in the desk's sandboxed
    page frame, and its own CSP allows no scripts, since the text comes from whoever wrote on the PR."""
    import html as H
    e = H.escape
    talk = []
    for c in view.get("comments") or []:
        talk.append((c.get("createdAt", ""), (c.get("author") or {}).get("login"), "commented", "", c.get("body") or ""))
    for r in view.get("reviews") or []:
        if r.get("body") or r.get("state") != "COMMENTED":
            talk.append((r.get("submittedAt", ""), (r.get("author") or {}).get("login"), (r.get("state") or "").lower().replace("_", " "), "", r.get("body") or ""))
    for c in view.get("inline") or []:
        line = c.get("line") or c.get("original_line")
        talk.append((c.get("created_at", ""), (c.get("user") or {}).get("login"), "commented on",
                     f"{c.get('path')}:{line}" + (" (outdated)" if c.get("line") is None else ""), c.get("body") or ""))
    talk.sort(key=lambda t: t[0])
    rendered = render_markdown(p, [view.get("body") or ""] + [t[4] for t in talk])
    body_html = rendered[0] if (view.get("body") or "").strip() else "<p class='empty'>No description provided.</p>"
    state = "draft" if p["draft"] else p["state"].lower()
    head = f"{p['head_repo']}:{p['head_ref']}" if p["head_repo"] and p["head_repo"] != f"{p['owner']}/{p['repo']}" else p["head_ref"]
    cards = "".join(
        f"<article class='card'><div class='who'><span>@{e(who or '?')}</span><span>{e(what)}</span>"
        + (f"<span class='where'>{e(where)}</span>" if where else "")
        + f"<span class='when'>{e(at[:10])}</span></div><div class='md'>{html}</div></article>"
        for (at, who, what, where, _), html in zip(talk, rendered[1:]))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; img-src https: data:">
<title>PR #{p['number']}</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>{PAGE_CSS}</style></head>
<body><main>
<div class="eyebrow"><a href="{e(p['url'])}" target="_blank" rel="noopener noreferrer">{e(p['owner'])}/{e(p['repo'])}#{p['number']} ↗</a><span class="state {e(state)}">{e(state)}</span></div>
<h1>{e(p['title'])}</h1>
<div class="facts"><span>@<b>{e(p['author'] or '?')}</b></span><span><b>{e(p['base_ref'])}</b> ← <b>{e(head)}</b></span>
<span>{plural(view.get('changedFiles'), 'file')} <span class="add">+{view.get('additions', '?')}</span> <span class="del">-{view.get('deletions', '?')}</span></span>
<span>head {e(p['head_sha'][:10])}</span></div>
<article class="card"><div class="md">{body_html}</div></article>
<h2>Discussion · {len(talk)}</h2>
{cards or "<p class='empty'>No comments yet.</p>"}
</main></body></html>
"""


# ------------------------------------------------------------------ posting
def anchor_of(item: dict) -> tuple[str, int, int] | None:
    m = re.match(r"^(.+?):(\d+)(?:-(\d+))?$", item.get("anchor") or "")
    if not m:
        return None
    a = int(m[2])
    return m[1], a, int(m[3] or a)


def commentable(p: dict, path: str) -> list[tuple[int, int]]:
    """New-side line ranges GitHub accepts inline comments on: the hunks of head vs merge-base."""
    try:
        patch = git(Path(p["clone"]), "diff", "-M", "-U3", p["merge_base"], p["head_sha"], "--", path)
    except DeskError:
        return []
    hunks, _ = gitdiff.parse(patch)
    return [(h["new_start"], h["new_start"] + h["new_len"] - 1) for h in hunks if h["new_len"]]


def comment_body(item: dict) -> str:
    parts = [f"**{item['title']}**"]
    if item.get("detail") and item["detail"] != item["title"]:
        parts.append(item["detail"].strip())
    if item.get("patch"):
        parts.append("Suggested patch:\n\n```diff\n" + item["patch"].strip("\n") + "\n```")
    return "\n\n".join(parts)


def review_payload(p: dict, items: list[dict], event: str) -> tuple[dict, list[dict]]:
    """One review: inline comments where the anchor is in the PR's diff, the rest listed in the body."""
    comments, loose, placed = [], [], []
    cache: dict[str, list] = {}
    for i in items:
        a = anchor_of(i)
        spot = None
        if a:
            path, start, end = a
            ranges = cache.setdefault(path, commentable(p, path))
            hunk = next(((lo, hi) for lo, hi in ranges if lo <= end <= hi), None) or next(((lo, hi) for lo, hi in ranges if lo <= start <= hi), None)
            if hunk:
                lo, hi = hunk
                line, first = min(end, hi), max(start, lo)
                spot = {"path": path, "line": line, "side": "RIGHT", "body": comment_body(i)}
                if first < line:
                    spot |= {"start_line": first, "start_side": "RIGHT"}
        if spot:
            comments.append(spot)
            placed.append({"id": i["id"], "where": f"{spot['path']}:{spot.get('start_line', spot['line'])}" + (f"-{spot['line']}" if spot.get("start_line") else ""), "title": i["title"]})
        else:
            loose.append(i)
            placed.append({"id": i["id"], "where": "review body" + (f" ({i['anchor']} is outside the PR's diff)" if i.get("anchor") else ""), "title": i["title"]})
    body = ""
    if loose:
        body = "\n\n---\n\n".join((f"`{i['anchor']}`\n\n" if i.get("anchor") else "") + comment_body(i) for i in loose)
    payload = {"commit_id": p["head_sha"], "event": event, "body": body, "comments": comments}
    return payload, placed


def post(p: dict, payload: dict) -> str:
    out = api(p, f"repos/{p['owner']}/{p['repo']}/pulls/{p['number']}/reviews", "-X", "POST", "--input", "-", input=json.dumps(payload))
    return json.loads(out).get("html_url") or p["url"]


def post_hint(sid: str) -> str:
    return run(f"pr {sid} post <ids> --dry-run") + " shows where each item lands"
