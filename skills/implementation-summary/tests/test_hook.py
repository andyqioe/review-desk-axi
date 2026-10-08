"""The activation hook: when it demands a summary, and the base it names so committed work is not left out.

Each test drives hooks/activate.py as Claude Code does (JSON on stdin) against a temporary repository,
with synthetic transcripts and its own state directory (TMPDIR).

  python3 -m unittest tests.test_hook -v
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "activate.py"
sys.path.insert(0, str(HOOK.parent))
import activate  # noqa: E402


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


class Hook(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="impl-summary-hook-")).resolve()
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
            git(self.repo, *args)
        (self.repo / "a.py").write_text("a = 1\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "base")
        self.start = git(self.repo, "rev-parse", "HEAD")
        self.transcript = self.tmp / "t.jsonl"
        self.entries: list[dict] = []
        self.session = "s1"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ helpers
    def user(self, text="do it"):
        self.entries.append({"type": "user", "uuid": f"u{len(self.entries)}", "origin": {"kind": "human"},
                             "message": {"content": text}})
        self.flush()
        return self.hook("prompt", {"prompt": text})

    def tool(self, name: str, **inp):
        self.entries.append({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]}})
        self.flush()
        if name == "Bash":
            return self.hook("post-bash", {"tool_name": name, "tool_input": inp})
        if name in activate.EDIT_TOOLS:
            return self.hook("post-edit", {"tool_name": name, "tool_input": inp})
        return None

    def flush(self):
        self.transcript.write_text("".join(json.dumps(e) + "\n" for e in self.entries))

    def hook(self, mode: str, extra: dict | None = None):
        data = {"session_id": self.session, "transcript_path": str(self.transcript), "cwd": str(self.repo), **(extra or {})}
        r = subprocess.run([sys.executable, str(HOOK), mode], input=json.dumps(data), capture_output=True, text=True,
                           env={**os.environ, "TMPDIR": str(self.state)})
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout) if r.stdout.strip() else None

    def stop(self):
        return self.hook("stop", {"stop_hook_active": False})

    def commit(self, msg: str, **files: str):
        for name, text in files.items():
            (self.repo / name).write_text(text)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", msg)

    # -------------------------------------------------------------- tests
    def test_bash_only_edits_demand_a_summary(self):
        self.user()
        self.tool("Bash", command="sed -i '' 's/1/2/' a.py")
        (self.repo / "a.py").write_text("a = 2\n")  # what the sed did
        out = self.stop()
        self.assertEqual(out["decision"], "block")
        self.assertIn("tracked files changed through Bash", out["reason"])
        self.assertIn(f"--base {self.start}", out["reason"])
        self.assertIn("0 commits since, plus uncommitted changes", out["reason"])

    def test_commit_only_turn_names_the_base_and_counts_commits(self):
        self.user()
        self.commit("one", **{"b.py": "b = 1\n"})
        out = self.tool("Bash", command=f"cd {self.repo} && GIT_INDEX_FILE=.git/tmp git -c user.name=x commit -m 'one; two'")
        self.assertIn(f"--base {self.start}", out["hookSpecificOutput"]["additionalContext"])
        out = self.stop()
        self.assertIn("1 git commit", out["reason"])
        self.assertIn("(1 commit since)", out["reason"])
        self.assertNotIn("per-commit mode", out["reason"])

    def test_work_across_turns_keeps_its_first_base_and_switches_to_per_commit(self):
        self.user("implement step 1")
        self.tool("Edit", file_path=str(self.repo / "a.py"))
        self.commit("step 1", **{"a.py": "a = 3\n"})
        self.tool("Bash", command="git commit -am 'step 1'")
        self.assertEqual(self.stop()["decision"], "block")  # nobody summarized yet
        self.user("now step 2")
        self.commit("step 2", **{"c.py": "c = 1\n"})
        self.tool("Bash", command="git commit -am 'step 2'")
        out = self.stop()
        self.assertIn(f"--base {self.start}", out["reason"])  # not HEAD at the second prompt
        self.assertIn("(2 commits since)", out["reason"])
        self.assertIn("per-commit mode", out["reason"])

    def test_running_the_skill_clears_the_base(self):
        self.user()
        self.tool("Edit", file_path=str(self.repo / "a.py"))
        self.commit("work", **{"a.py": "a = 4\n"})
        self.tool("Skill", skill="implementation-summary")
        self.assertIsNone(self.stop())
        self.assertNotIn("bases", json.loads((self.state / "implementation-summary-hook" / "s1.json").read_text()))
        self.user("next")
        self.tool("Edit", file_path=str(self.repo / "a.py"))
        self.assertIn(f"--base {git(self.repo, 'rev-parse', 'HEAD')}", self.stop()["reason"])

    def test_research_turns_and_tool_output_do_not_count(self):
        self.user("what does a.py do?")
        self.tool("Bash", command="cat a.py && python3 -m pytest")
        (self.repo / "out.log").write_text("untracked tool output\n")
        self.assertIsNone(self.stop())

    def test_an_edit_in_another_repository_records_that_repository(self):
        other = self.repo / "vendor" / "other"  # inside the session cwd: temp dirs outside it are scratch
        other.mkdir(parents=True)
        git(other, "init", "-q", "-b", "main")
        git(other, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "root")
        head = git(other, "rev-parse", "HEAD")
        self.user()
        self.tool("Write", file_path=str(other / "new" / "x.py"))
        out = self.stop()
        self.assertIn(f"Work in {other} began at {head[:10]}", out["reason"])
        self.assertNotIn(f"Work in {self.repo} ", out["reason"])

    def test_commit_dirs_parsing(self):
        cd = activate.commit_dirs
        self.assertEqual(cd("git commit -m x", "/r"), ["/r"])
        self.assertEqual(cd("cd sub && git -C deep -c a=b commit -qm 'x'", "/r"), ["/r/sub/deep"])
        self.assertEqual(cd("GIT_INDEX_FILE=/tmp/i git commit -m 'a; b'", "/r"), ["/r"])
        self.assertEqual(cd("git log --grep commit; echo git commit", "/r"), [])
        self.assertEqual(cd("git commit-tree x", "/r"), [])


if __name__ == "__main__":
    unittest.main()
