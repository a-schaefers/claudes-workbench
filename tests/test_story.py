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
STATES = ("off", "none", "idle", "working", "waiting")


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
        host = Path("mod") / "hooks" / "story.ts"
        skill = Path("skill") / "workbench"
        for rel, old, new in ((page, "Message {who}", "Message {who}!"),
                              (host, "and end your turn.", "and end your turn"),
                              (skill / "SKILL.md", "never undo a change the user made there", "never undo a change"),
                              (skill / "reference" / "driving.md", "it clears when the prompt is answered", "it clears")):
            with self.subTest(copy=str(rel)), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                for f in (page, host, *map(Path, story.DOCS)):
                    (root / f).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy(ROOT / f, root / f)
                with mock.patch.object(gen_story, "ROOT", root), mock.patch.object(gen_story, "PAGE_TS", root / page), \
                        mock.patch.object(gen_story, "HOST_TS", root / host):
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

    def test_the_hosts_module_says_the_refusals_each_host_said_before_it_byte_for_byte(self):
        # #1355: the mod's AWAY_QUESTION, AWAY_PLAN and CONSENT_WAITING and the plugin's
        # AWAY_QUESTION moved into the story; the mod's tests and the turns that read them know
        # these bytes, and the plugin's names its reply tool (opencode-plugin/chat.ts REPLY_ID)
        self.assertEqual(story.text("refusal.question", "claude-code"),
                         "The user is away: ask in the web chat (`wb chat reply`) and end your turn.")
        self.assertEqual(story.text("refusal.plan", "claude-code"),
                         "Post the plan in the web chat and end your turn; approval is at the terminal.")
        self.assertEqual(story.text("refusal.consent", "claude-code"),
                         "Claude is waiting on a workbench dialog in the terminal: answer it there.")
        self.assertEqual(story.text("refusal.question", "opencode"),
                         "The user is away: ask in the web chat (the workbench_chat_reply tool) and end your turn.")
        with self.assertRaises(ValueError):  # OpenCode has no plan dialog: Plan is an agent
            story.text("refusal.plan", "opencode")
        reg = (ROOT / "mod" / "hooks" / "register.tsx").read_text()
        for name, key in (("AWAY_QUESTION", "refusal.question"), ("AWAY_PLAN", "refusal.plan"),
                          ("CONSENT_WAITING", "refusal.consent")):
            with self.subTest(name=name):
                self.assertRegex(reg, rf"const {name}: string = SAY\['{re.escape(key)}'\]")
        self.assertIn("export const AWAY_QUESTION: string = SAY['refusal.question']",
                      (ROOT / "opencode-plugin" / "chat.ts").read_text())

    def test_the_skill_blocks_are_claude_codes_and_the_hosts_module_has_opencodes(self):
        # #1355: SKILL.md and driving.md carry Claude Code's sentence; opencode-plugin/skill.ts
        # swaps in OpenCode's from mod/hooks/story.ts, so its rewrite can't fall behind a reword
        for rel, blocks in story.DOCS.items():
            if not rel.startswith("skill/"):
                continue
            body = (ROOT / rel).read_text()
            for key, driver in blocks:
                with self.subTest(rel=rel, key=key):
                    self.assertEqual(driver, "claude-code")
                    self.assertIn(f"<!-- story:{key}:claude-code -->\n{story.text(key, driver)}\n<!-- /story -->\n", body)
                    self.assertIn(key, story.HOST)
                    self.assertIn(json.dumps(story.text(key, "opencode"), ensure_ascii=False), gen_story.host_ts())
                    self.assertNotIn("Claude", story.text(key, "opencode"))

    def test_every_key_has_a_reader(self):
        src = "\n".join(p.read_text() for p in sorted((ROOT / "workbench").glob("*.py")) if p.name != "story.py")
        hosts = "\n".join(p.read_text() for p in [ROOT / "mod" / "hooks" / "register.tsx",
                                                     *sorted((ROOT / "opencode-plugin").glob("*.ts"))])
        page = (ROOT / "workbench" / "chat_static" / "src" / "app.ts").read_text() \
            + (ROOT / "workbench" / "chat_static" / "index.html").read_text()
        docs = {k for blocks in story.DOCS.values() for k, _ in blocks}
        computed = {f"state.{s}" for s in STATES} | {f"next.{s}" for s in STATES} \
            | {f"wait.{k}" for k in chat.WAIT_KINDS}
        self.assertTrue(computed <= set(story.TEXTS), computed - set(story.TEXTS))
        for key in story.TEXTS:
            with self.subTest(key=key):
                read = (f'story.text("{key}"' in src or f"'{key}'" in page or f'data-story="{key}"' in page
                        or key in docs or key in computed or (key in story.HOST and f"SAY['{key}']" in hosts))
                self.assertTrue(read, f"{key}: nothing reads it")
                if key in story.PAGE:
                    self.assertTrue(f"'{key}'" in page or f'data-story="{key}"' in page, f"{key}: in PAGE, unread there")

    def test_the_pages_text_before_a_state_arrives_is_the_storys_for_any_driver(self):
        body = (ROOT / "workbench" / "chat_static" / "index.html").read_text()
        found = re.findall(r'<(\w+)[^>]*data-story="([a-z.]+)"[^>]*>(.*?)</\1>', body, re.S)
        self.assertGreaterEqual(len(found), 5)  # the hint, the intro, How the chat works' three lines (2026-11-26)
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
