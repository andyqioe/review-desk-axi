"""ste_check.py: each STE rule warns on a sentence that breaks it, and stays quiet on code, quotes and names.

  python3 -m unittest tests.test_ste -v
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import ste_check  # noqa: E402


def rules(text: str, kind: str = "markdown") -> list[str]:
    return [n.rule for n in ste_check.check(text, kind).notes]


class Rules(unittest.TestCase):
    def test_each_rule_fires(self):
        cases = {
            "length": "The worker reads the queue and then it writes every record to the store and the store sends one reply to "
                      "the client for each record that it got.",
            "passive": "The charge is sent to the bank.",
            "tense": "The hook has recorded the base.",
            "hedge": "A retry might charge the customer twice.",
            "ing": "The worker stops after restarting the queue.",
            "phrasal": "The agent can set up the desk.",
            "word": "The tool can utilize the cache.",
            "semicolon": "The worker stops; the queue keeps the job.",
            "instructions": "Run the tests, then open the desk.",
        }
        for rule, sentence in cases.items():
            with self.subTest(rule=rule):
                self.assertIn(rule, rules(sentence))

    def test_instruction_limit_is_twenty_words(self):
        twenty_one = "Run " + " ".join(["the"] * 19) + " tests."
        self.assertIn("length", rules(twenty_one))
        self.assertNotIn("length", rules("The " + " ".join(["worker"] * 19) + " stops."))  # 21 words, descriptive

    def test_paragraph_limit(self):
        para = "\n".join(f"The worker stops queue {i}." for i in range(7))
        self.assertIn("paragraph", rules(para))
        self.assertNotIn("paragraph", rules("\n".join(f"The worker stops queue {i}." for i in range(6))))

    def test_clean_text_stays_quiet(self):
        text = """# Heading with a long title that is not a sentence and must not count as one at all

The client sends a charge to the bank.
If the build fails, read the first error.

- **Risk**: the worker crashes, and nobody knows what the bank got.
- **Mechanism**: `send_charge_after_recording()` saves the intent first (`src/pay.py:40-42`).

```python
# code is never checked: it is being utilized and has been set up
x = 1
```

| a table row is skipped | it is being utilized |
"""
        self.assertEqual(rules(text), [])

    def test_quotes_names_and_markers_are_mentions(self):
        self.assertEqual(rules('Write "use", not "utilize", and "start", not "set up".'), [])
        self.assertEqual(rules("The `existing_cache` keeps a string for the logging setting."), [])
        self.assertEqual(rules("<!-- ste: off -->\nIt has been utilized; it might be sent.\n<!-- ste: on -->\nThe tool stops."), [])

    def test_abbreviations_do_not_split_sentences(self):
        rep = ste_check.check("The page keeps names, e.g. a sha, in the label.")
        self.assertEqual(rep.sentences, 1)
        self.assertIn("word", [n.rule for n in rep.notes])

    def test_html_reads_text_blocks_and_skips_code(self):
        html = """<section><h2>Is being utilized</h2>
<p class="lead">The worker stops the queue.</p>
<ul class="points"><li data-label="risk">The charge is sent twice.</li></ul>
<pre class="scene">it has been set up ──▶ utilized</pre>
<p>The tool reads <code>being_utilized()</code> at <a data-loc="src/x.py:4"></a>.</p></section>"""
        rep = ste_check.check(html, "html")
        self.assertEqual(rep.sentences, 3)
        self.assertEqual([(n.rule, n.line) for n in rep.notes], [("passive", 3)])

    def test_line_numbers_point_at_the_sentence(self):
        rep = ste_check.check("The tool stops.\nThe charge is sent.\n\n- A retry might fail.")
        self.assertEqual([(n.rule, n.line) for n in rep.notes], [("passive", 2), ("hedge", 4)])


class Cli(unittest.TestCase):
    def run_cli(self, *args, input=None):
        return subprocess.run([sys.executable, str(SCRIPTS / "ste_check.py"), *args], input=input, capture_output=True, text=True)

    def test_warns_by_default_and_strict_fails(self):
        r = self.run_cli("-", input="The charge is sent.\n")
        self.assertEqual(r.returncode, 0)
        self.assertIn("ste: 1 warning in 1 sentences (stdin)", r.stdout)
        self.assertEqual(self.run_cli("--strict", "-", input="The charge is sent.\n").returncode, 1)
        self.assertEqual(self.run_cli("--strict", "-", input="The bank gets the charge.\n").returncode, 0)

    def test_json(self):
        r = self.run_cli("--format", "json", "-", input="The charge is sent.\n")
        d = json.loads(r.stdout)
        self.assertEqual((d["sentences"], d["warnings"][0]["rule"]), (1, "passive"))

    def test_the_guide_follows_its_own_rules(self):
        guide = SCRIPTS.parent / "STE.md"
        self.assertEqual(ste_check.check(guide.read_text()).notes, [])


if __name__ == "__main__":
    unittest.main()
