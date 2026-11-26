"""The story (#1355): workbench/story.py is the one source of every sentence the chat, the web
app and the docs tell the user about the driver, and scripts/gen_story.py's copies are what it
says. A real file system for the generator's check; node, when there is one, for the page's fill."""
import html
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from workbench import chat, story  # noqa: E402
import gen_story  # noqa: E402

FIXTURE = json.loads((ROOT / "tests" / "fixtures" / "story" / "fill.json").read_text())["cases"]
# the keys read by a computed name: chat.driver_state's `state.<state>`, `next.<state>` and
# `wait.<kind>` (chat.WAIT_KINDS)
STATES = ("none", "idle", "working", "waiting")


def purs_strings(code: str) -> list[str]:
    """The string literals of PureScript source ``code``: outside `--` comments and char literals."""
    out, i, n = [], 0, len(code)
    while i < n:
        c = code[i]
        if code.startswith("--", i):
            i = code.find("\n", i)
            i = n if i < 0 else i
        elif c == "'" and i + 2 < n and (code[i + 2] == "'" or (code[i + 1] == "\\" and code[i + 3:i + 4] == "'")):
            i += 3 if code[i + 2] == "'" else 4
        elif c == '"':
            j, buf = i + 1, []
            while j < n and code[j] != '"':
                buf.append(code[j:j + 2] if code[j] == "\\" else code[j])
                j += 2 if code[j] == "\\" else 1
            out.append("".join(buf))
            i = j + 1
        else:
            i += 1
    return out


class Story(unittest.TestCase):
    def test_the_generated_copies_are_what_story_py_says(self):
        self.assertEqual(gen_story.check(), [])

    def test_a_one_byte_edit_to_a_generated_copy_fails_the_check(self):
        page = Path("workbench") / "chat_static" / "src" / "story.ts"
        for rel, old, new in ((page, "Message {who}", "Message {who}!"),
                              (Path("README.md"), "never undoes a change you made", "never undoes a change")):
            with self.subTest(copy=str(rel)), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                for f in (page, Path("README.md")):
                    (root / f).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy(ROOT / f, root / f)
                with mock.patch.object(gen_story, "ROOT", root), mock.patch.object(gen_story, "PAGE_TS", root / page):
                    self.assertEqual(gen_story.check(), [])
                    body = (root / rel).read_text()
                    self.assertIn(old, body)
                    (root / rel).write_text(body.replace(old, new, 1))
                    [bad] = gen_story.check()
                    self.assertEqual(bad, f"{rel.as_posix()}: not what workbench/story.py says "
                                          "(python3 scripts/gen_story.py, and commit it)")

    def test_fill_gives_the_fixtures_answers(self):
        for c in FIXTURE:
            with self.subTest(t=c["t"], fields=c["fields"]):
                if c.get("error"):
                    with self.assertRaises(KeyError):
                        story.fill(c["t"], c["fields"], c.get("partial", False))
                else:
                    self.assertEqual(story.fill(c["t"], c["fields"], c.get("partial", False)), c["out"])

    def test_the_pages_fill_gives_the_same_answers_as_pythons(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("no node")
        script = (ROOT / "workbench" / "chat_static" / "story.js").read_text()
        prog = ("const vm = require('node:vm'); const fs = require('node:fs');"
                "const ctx = {}; vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);"
                "const cases = JSON.parse(fs.readFileSync(process.argv[2], 'utf8')).cases;"
                "console.log(JSON.stringify(cases.map(c => { try { return { out: ctx.storyFill(c.t, c.fields, !!c.partial) } }"
                " catch (e) { return { error: true } } })))")
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "story.js").write_text(script)
            out = subprocess.run([node, "-e", prog, str(Path(tmp) / "story.js"),
                                  str(ROOT / "tests" / "fixtures" / "story" / "fill.json")],
                                 capture_output=True, text=True, check=True).stdout
        self.assertEqual(json.loads(out), [{"error": True} if c.get("error") else {"out": c["out"]} for c in FIXTURE])

    def test_every_key_renders_for_every_driver_it_is_said_for(self):
        fields = {"where": "~/p", "what": "a question", "tool": "Bash"}
        for key in story.TEXTS:
            for driver in story.DRIVERS:
                with self.subTest(key=key, driver=driver):
                    if driver not in story.ONLY.get(key, tuple(story.DRIVERS)):
                        with self.assertRaises(ValueError):
                            story.text(key, driver, **fields)
                        continue
                    said = story.text(key, driver, **fields)
                    self.assertNotIn("{", said)
                    self.assertTrue(said and said[0] == said[0].upper() or key.startswith("wait."), said)
                    if driver == "opencode":
                        self.assertNotIn("Claude", said)

    def test_unknown_keys_drivers_and_fields_raise(self):
        with self.assertRaises(KeyError):
            story.text("state.asleep")
        with self.assertRaises(KeyError):
            story.text("purpose", "cursor")
        with self.assertRaises(KeyError):
            story.text("state.idle", "claude-code")  # {where} unfilled
        self.assertEqual(story.driver_of("claude"), "claude-code")
        self.assertEqual(story.driver_of("opencode"), "opencode")
        self.assertEqual(story.driver_of(None), "any")

    def test_the_waiting_rows_keep_the_bytes_the_mod_the_pushes_and_the_tests_know(self):
        self.assertEqual(chat.WAITING, "Claude is waiting for you in the terminal.")
        self.assertEqual(chat.WAITING_GRANT, " Run it with permissions granted to work from the chat.")
        self.assertEqual(story.text("note.waiting", "opencode"), "OpenCode is waiting for you in the terminal.")

    def test_every_key_has_a_reader(self):
        src = "\n".join(p.read_text() for p in sorted((ROOT / "workbench").glob("*.py")) if p.name != "story.py")
        page = (ROOT / "workbench" / "chat_static" / "src" / "app.ts").read_text() \
            + (ROOT / "workbench" / "chat_static" / "index.html").read_text()
        docs = {k for blocks in story.DOCS.values() for k, _ in blocks}
        computed = {f"state.{s}" for s in STATES} | {f"next.{s}" for s in STATES} \
            | {f"wait.{k}" for k in chat.WAIT_KINDS}
        self.assertTrue(computed <= set(story.TEXTS), computed - set(story.TEXTS))
        for key in story.TEXTS:
            with self.subTest(key=key):
                read = (f'story.text("{key}"' in src or f"'{key}'" in page or f'data-story="{key}"' in page
                        or key in docs or key in computed)
                self.assertTrue(read, f"{key}: nothing reads it")
                if key in story.PAGE:
                    self.assertTrue(f"'{key}'" in page or f'data-story="{key}"' in page, f"{key}: in PAGE, unread there")

    def test_the_pages_text_before_a_state_arrives_is_the_storys_for_any_driver(self):
        body = (ROOT / "workbench" / "chat_static" / "index.html").read_text()
        found = re.findall(r'<(\w+)[^>]*data-story="([a-z.]+)"[^>]*>(.*?)</\1>', body, re.S)
        self.assertGreaterEqual(len(found), 6)
        for _, key, inner in found:
            with self.subTest(key=key):
                self.assertIn(key, story.PAGE)
                plain = " ".join(html.unescape(re.sub(r"</?code>", "`", inner)).split())
                self.assertEqual(plain, story.text(key, "any"))

    def test_no_sentence_lives_in_the_kernel(self):
        # #1355, amending #1356: decisions return keys and facts; a wording patch needs no
        # PureScript toolchain. A string of four or more words in hand-written kernel/src fails.
        for f in sorted((ROOT / "kernel" / "src").rglob("*.purs")):
            if f.name == "Tables.purs":  # generated from wb's Python, its one sentence NO_ANSWER's opening
                continue
            for s in purs_strings(f.read_text()):
                with self.subTest(file=f.name, s=s):
                    self.assertLess(len(s.split()), 4, f"{f.name}: a sentence in the kernel: {s!r}")


if __name__ == "__main__":
    unittest.main()
