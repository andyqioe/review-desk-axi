"""Build a git repo whose working tree holds a large change set with every awkward case.

Base commit on main, then on branch `feature`: committed work, staged work, unstaged work
and untracked files, so the default base (merge-base with main) has to include all four.
Random edits are seeded, so a failure reproduces.
"""

from __future__ import annotations

import os
import random
import subprocess
from pathlib import Path


def sh(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "core.quotePath=false", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout


def py_module(name: str, fns: int) -> str:
    out = [f'"""module {name}"""', "import os", ""]
    for i in range(fns):
        out += [f"def fn_{i}(x):", f'    """doc {i}"""', f"    y = x + {i}", f"    if y > {i * 3}:",
                "        return y * 2", "    return y", ""]
    out += ["class Thing:", "    def method_a(self):", "        return 1", "", "    def method_b(self):", "        return 2", ""]
    return "\n".join(out) + "\n"


def rs_module(n: int) -> str:
    body = [f"    pub fn f{i}(&self) -> u32 {{\n        self.n + {i}\n    }}\n" for i in range(n)]
    return "\n".join(["pub struct Engine { n: u32 }", "impl Engine {", *body, "}", ""]) + "\n"


def ts_module(n: int) -> str:
    return "\n".join([f"export function h{i}(a: number): number {{\n  return a * {i};\n}}\n" for i in range(n)]) + "\n"


def md_doc(n: int) -> str:
    return "\n".join([f"## Section {i}\n\nParagraph {i} explains a thing.\nIt has two sentences.\n" for i in range(n)]) + "\n"


def random_edit(rng: random.Random, text: str, edits: int) -> str:
    """Insert, delete and replace runs at random spots, including the first and last lines."""
    lines = text.split("\n")
    for k in range(edits):
        if not lines:
            break
        op = rng.choice(["ins", "del", "rep", "rep"])
        at = 0 if k == 0 else (len(lines) - 1 if k == 1 else rng.randrange(len(lines)))
        span = rng.randint(1, 6)
        if op == "ins":
            lines[at:at] = [f"# inserted {k}.{j}" for j in range(span)]
        elif op == "del":
            del lines[at:at + span]
        else:
            lines[at:at + span] = [f"    replaced = {k}  # {j}" for j in range(rng.randint(1, span))]
    return "\n".join(lines)


def build(root: Path, scale: int = 240, seed: int = 7) -> dict:
    """Create the repo at `root` and return facts the tests assert on."""
    rng = random.Random(seed)
    root.mkdir(parents=True)
    sh(root, "init", "-q", "-b", "main")
    sh(root, "config", "user.email", "t@example.com")
    sh(root, "config", "user.name", "t")
    sh(root, "config", "commit.gpgsign", "false")

    def w(rel: str, data: str | bytes, newline: str | None = None) -> None:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            p.write_bytes(data)
        else:
            with open(p, "w", encoding="utf-8", newline=newline) as fh:
                fh.write(data)

    bulk = []
    for i in range(scale):
        d = f"pkg{i % 12}/sub{i % 5}"
        kind = i % 4
        rel = f"{d}/m{i}." + ["py", "rs", "ts", "md"][kind]
        w(rel, [py_module(str(i), 12 + i % 9), rs_module(10 + i % 7), ts_module(12 + i % 5), md_doc(8 + i % 4)][kind])
        bulk.append(rel)
    w("src/util/old_name.py", py_module("old", 30))
    w("src/util/moved.py", py_module("moved", 10))
    w("src/util/to_delete.py", py_module("del", 5))
    w("src/util/to_delete_unstaged.py", py_module("del2", 5))
    w("src/util/mv_plain.py", py_module("plain", 15))
    w("src/util/gone_binary.bin", bytes(range(256)) * 3)
    w("lib/engine.rs", rs_module(60))
    w("web/app.ts", ts_module(50))
    w("docs dir/read me.md", "# Title\n\nline\n")
    w("crlf.txt", "a\r\nb\r\nc\r\n", newline="")
    w("noeol.txt", "x\ny")
    w("script.sh", "#!/bin/sh\necho hi\n")
    w("logo.png", bytes(range(256)) * 4)
    w("typechange", "was a file\n")
    sh(root, "add", "-A")
    sh(root, "commit", "-qm", "base")

    sh(root, "checkout", "-qb", "feature")
    # committed on the branch
    sh(root, "mv", "src/util/old_name.py", "src/util/new_name.py")
    p = root / "src/util/new_name.py"
    p.write_text(p.read_text().replace("y = x + 3\n", "y = x + 333\n"))
    (root / "src/helpers").mkdir(parents=True)
    sh(root, "mv", "src/util/moved.py", "src/helpers/moved.py")
    committed = rng.sample(bulk, scale // 6)
    for rel in committed:
        (root / rel).write_text(random_edit(rng, (root / rel).read_text(), 3))
    sh(root, "add", "-A")
    sh(root, "commit", "-qm", "branch work")

    # staged
    sh(root, "rm", "-q", "src/util/to_delete.py")
    s = (root / "lib/engine.rs").read_text()
    (root / "lib/engine.rs").write_text(s.replace("self.n + 10\n", "self.n + 10 + extra()\n").replace("self.n + 50\n", "self.n * 50\n"))
    os.chmod(root / "script.sh", 0o755)
    staged = rng.sample([b for b in bulk if b not in committed], scale // 8)
    for rel in staged:
        (root / rel).write_text(random_edit(rng, (root / rel).read_text(), 2))
    sh(root, "add", "-A")

    # unstaged
    (root / "src/util/to_delete_unstaged.py").unlink()
    lines = (root / "web/app.ts").read_text().split("\n")
    del lines[0:8]
    lines = lines[:40] + ["// inserted block"] + [f"export const added{i} = (v: number) => v + {i};" for i in range(30)] + lines[40:]
    (root / "web/app.ts").write_text("\n".join(lines[:-10]))
    unstaged = rng.sample(bulk, scale // 3)  # overlaps committed and staged on purpose
    for rel in unstaged:
        (root / rel).write_text(random_edit(rng, (root / rel).read_text(), rng.randint(1, 9)))
    with open(root / "docs dir/read me.md", "a") as fh:
        fh.write("more\n")
    w("crlf.txt", "a\r\nB\r\nc\r\n", newline="")
    w("noeol.txt", "x\nz")
    w("logo.png", bytes(range(255, -1, -1)) * 4)
    (root / "typechange").unlink()
    os.symlink("src/util/new_name.py", root / "typechange")
    (root / "src/util/gone_binary.bin").unlink()
    # plain `mv` + small edit: deleted tracked file + untracked file the tool should pair
    plain = (root / "src/util/mv_plain.py").read_text()
    (root / "src/util/mv_plain.py").unlink()
    w("src/moved_by_mv/plain.py", plain.replace("y = x + 4\n", "y = x + 44\n"))

    # untracked
    w("src/new_pkg/deep/fresh.py", "def brand_new():\n    return 1\n\nclass Fresh:\n    pass\n")
    w("spaced dir/naïve file.py", "x = 1\n")
    w("src/new_pkg/empty.py", "")
    w("src/new_pkg/blob.bin", b"\x00\x01\x02binary")
    os.symlink("../lib", root / "src/new_pkg/link_to_lib")
    w("big/generated.ts", ts_module(400))
    nested = root / "vendor/nested"
    nested.mkdir(parents=True)
    sh(nested, "init", "-q")
    w("vendor/nested/inner.txt", "inner\n")
    w(".lavish/impl-summary-old.html", "<html>earlier page</html>\n")
    w(".gitignore", "ignored.log\n")
    w("ignored.log", "noise\n")

    return {
        "bulk": bulk, "committed": committed, "staged": staged, "unstaged": unstaged,
        "paired": ("src/util/mv_plain.py", "src/moved_by_mv/plain.py"),
    }


if __name__ == "__main__":
    import sys
    import tempfile
    dest = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp()) / "repo"
    build(dest)
    print(dest)
