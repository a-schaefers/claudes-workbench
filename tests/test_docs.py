"""The docs say what the code does. Dated 2026-11-16, the user: "I don't want to have to do this
again", after a whole-codebase audit found CLAUDE.md and ARCHITECTURE.md months behind the code.
What a test can see is checked here: every command and flag is in ARCHITECTURE.md's command
reference, every module in CLAUDE.md's layout, every environment variable in CLAUDE.md. What it
can't (a default, a behaviour) is CLAUDE.md's rule: the commit that changes it updates the docs."""
import argparse
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from workbench import cli  # noqa: E402

CLAUDE = (ROOT / "CLAUDE.md").read_text()
ARCH = (ROOT / "ARCHITECTURE.md").read_text()


class _Built(Exception):
    pass


def wb_parser() -> argparse.ArgumentParser:
    """The parser cli.main builds, caught before it parses anything."""
    def catch(self, *a, **k):
        raise _Built(self)
    real = argparse.ArgumentParser.parse_args
    argparse.ArgumentParser.parse_args = catch
    try:
        cli.main([])
    except _Built as e:
        return e.args[0]
    finally:
        argparse.ArgumentParser.parse_args = real
    raise AssertionError("cli.main never built its parser")


def commands(parser, path=()):
    """(the command's words, each option's spellings) for every command and subcommand."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                opts = [a.option_strings for a in sub._actions
                        if a.option_strings and "--help" not in a.option_strings]
                yield (*path, name), opts
                yield from commands(sub, (*path, name))


def section(text: str, heading: str) -> str:
    start = text.index(heading)
    end = text.find("\n## ", start + len(heading))
    return text[start:end if end > 0 else None]


class DocsMatchTheCode(unittest.TestCase):
    """CLAUDE.md and ARCHITECTURE.md name everything a reader looks up."""

    def test_every_command_and_flag_is_in_the_command_reference(self):
        ref = section(ARCH, "## Every `wb` command")
        missing = []
        for words, opts in commands(wb_parser()):
            rows = [line for line in ref.splitlines() if f"wb {words[0]}" in line]
            if not rows or not all(any(w in r for r in rows) for w in words[1:]):
                missing.append("wb " + " ".join(words))
                continue
            for spellings in opts:  # -m or --message: either will do
                if not any(re.search(rf"`[^`]*{re.escape(o)}\b", r) for r in rows for o in spellings):
                    missing.append(f"wb {' '.join(words)} {spellings[-1]}")
        self.assertEqual(missing, [], "add these to ARCHITECTURE.md's *Every `wb` command*")

    def test_the_quickstart_finds_the_marketplace_where_claude_code_keeps_it(self):
        # the 2026-11-19 audit: ~/.claude hard-coded, wrong for CLAUDE_CONFIG_DIR users
        for text in ((ROOT / "README.md").read_text(), ARCH):
            setup = [line for line in text.splitlines() if "marketplaces/workbench/bin/wb setup" in line]
            self.assertTrue(setup)
            for line in setup:
                self.assertIn('"${CLAUDE_CONFIG_DIR:-$HOME/.claude}"/plugins/', line)

    def test_every_module_is_in_the_layout(self):
        layout = section(CLAUDE, "## Layout")
        missing = [p.name for p in sorted((ROOT / "workbench").glob("*.py"))
                   if p.name != "__init__.py" and f"`{p.name}`" not in layout]
        self.assertEqual(missing, [], "add these to CLAUDE.md's *Layout*")

    def test_every_environment_variable_is_in_claude_md(self):
        code = [*(ROOT / "workbench").glob("*.py"), ROOT / "mod/hooks/register.tsx"]
        names = {n for f in code for n in re.findall(r"\bWB_[A-Z_]+\b", f.read_text())}
        missing = sorted(n for n in names if f"`{n}`" not in CLAUDE)
        self.assertEqual(missing, [], "add these to CLAUDE.md's *State and config*")

    def test_the_readme_names_the_floors_the_code_holds(self):
        # #273 (the audit of 1.2): "OpenCode 2.x" while runner.OPENCODE_TESTED starts at 2.3, and
        # "16K at least" with OpenCode's own prompt about 10K of it
        from workbench import models, runner
        readme = (ROOT / "README.md").read_text()
        low = ".".join(map(str, runner.OPENCODE_TESTED[0]))
        self.assertIn(f"[OpenCode](https://opencode.ai) {low} or newer", readme)
        self.assertIn(f"{models.MIN_WINDOW // 1024}K at the least", readme)
        self.assertIn("32K context window recommended", readme)

    def test_the_panes_probe_row_names_the_line_count_the_code_holds(self):
        # the review of #857's off and on (2026-11-22): the pane's "On (large files only)" sentence
        # copies context.PROBE_FILE_LINES, which it can't import, so a change to one is caught here
        from workbench import context
        said = f"one function of a file over {context.PROBE_FILE_LINES:,} lines with probe"
        pane = (ROOT / "mod/hooks/register.tsx").read_text()
        stops = pane[pane.index("const PROBE_STOPS"):]
        self.assertIn(said, stops[:stops.index("]\n")], "the pane's PROBE_STOPS on sentence")
        self.assertIn(said, " ".join(ARCH.split()), "ARCHITECTURE.md's pane row")

    def test_wb_start_role_and_the_agents_switch_are_in_the_command_reference(self):
        # #1016: the role Claude picks per task, and the user's Agents switch
        ref = section(ARCH, "## Every `wb` command")
        start = next(line for line in ref.splitlines() if line.startswith("| `wb start "))
        self.assertIn("`--role significant\\|trivial\\|writing`", start)
        self.assertIn("`wb route all gpu\\|api\\|both`", ref)
        self.assertIn("--role", (ROOT / "skill/workbench/SKILL.md").read_text())

    def test_claude_md_stays_under_claude_codes_size_warning(self):
        # Claude Code warns past 40,000 characters, and every session that changes the code
        # loads this file (local agents too, when workbench delegates work on itself)
        self.assertLess(len(CLAUDE), 25_000, "move detail to ARCHITECTURE.md")


if __name__ == "__main__":
    unittest.main()
